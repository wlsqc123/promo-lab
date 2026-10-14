"""락 시간 초과·반대 순서 잠금·같은 순서 잠금을 별도 DB에서 비교."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from runner import settlement


def wait_for(check, message):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise RuntimeError(message)


def retry_logged(path, sqlstate):
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("sqlstate") == sqlstate and "retry" in item:
            return item
    return None


def experiment(scenario):
    run_id = uuid4().hex
    database = f"locking_{run_id}"
    folder = Path("results") / f"locking-{scenario}-{run_id}"
    folder.mkdir(parents=True)
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(os.environ["DATABASE_URL"]), "dbname": database})
    child = None
    report = {"scenario": scenario, "status": "failed", "database": database, "folder": str(folder)}
    log = folder / "worker.log"
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup:
                setup.execute(Path("sql/schema.sql").read_text(encoding="utf-8"))
                if scenario in ("deadlock", "ordered"):
                    # 실험 DB에서만 작업자의 감지 시간을 1초로 설정해 충돌을 만들 시간 확보.
                    setup.execute(sql.SQL("ALTER ROLE lab_worker IN DATABASE {} "
                                          "SET deadlock_timeout='1s'").format(sql.Identifier(database)))
            os.environ["ADMIN_DATABASE_URL"] = admin
            os.environ["DATABASE_URL"] = worker
            settlement.EXPECTED = folder / "expected.json"
            settlement.prepare(SimpleNamespace(seed=42, guilds=100, events=10000, chunk_size=10))
            expected = json.loads(settlement.EXPECTED.read_text(encoding="utf-8"))
            with psycopg.connect(admin, autocommit=True) as holder, \
                    psycopg.connect(admin, autocommit=True) as observer, \
                    log.open("w", encoding="utf-8") as output:
                with holder.transaction():
                    # 청크 끝 지갑을 잡으면 작업자가 앞선 9개 지갑을 갱신한 상태에서 대기.
                    first = 1 if scenario == "ordered" else 10
                    holder.execute("SET LOCAL lock_timeout='10s'")
                    holder.execute("SET LOCAL deadlock_timeout='5s'")
                    holder.execute("SELECT guild_id FROM guild_wallet WHERE guild_id=%s FOR UPDATE", (first,))
                    env = {**os.environ, "RESULTS_DIR": str(folder), "WORKER_NAME": "locking-worker"}
                    child = subprocess.Popen([sys.executable, "-m", "runner", "settle"], env=env,
                                             stdout=output, stderr=subprocess.STDOUT, text=True)
                    def blocked():
                        return observer.execute(
                            "SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid) "
                            "FROM pg_stat_activity WHERE datname=%s AND application_name='locking-worker' "
                            "AND %s=ANY(pg_blocking_pids(pid))",
                            (database, holder.info.backend_pid)).fetchone()
                    report["lock_observation"] = wait_for(blocked, "작업자의 잠금 대기를 확인하지 못했습니다.")
                    if scenario == "timeout":
                        report["first_retry"] = wait_for(
                            lambda: retry_logged(log, "55P03"), "락 시간 초과 재시도를 확인하지 못했습니다.")
                        # 실패한 청크의 결과·잔액·완료 기록이 다른 연결에 남지 않았는지 확인.
                        result_count = observer.execute("SELECT count(*) FROM settlement_result").fetchone()[0]
                        balances = observer.execute("SELECT balance FROM guild_wallet ORDER BY guild_id").fetchall()
                        completed = observer.execute("SELECT count(*) FROM settlement_chunk "
                                                     "WHERE completed_at IS NOT NULL").fetchone()[0]
                        if result_count != 0 or any(value != 0 for (value,) in balances) or completed != 0:
                            raise RuntimeError("시간 초과 후 청크 변경이 남았습니다.")
                        report["rollback_after_timeout"] = True
                    else:
                        # 반대 순서: 보조 연결 10→1, 정산 작업자 1→10. 같은 순서: 둘 다 1→10.
                        second = 10 if scenario == "ordered" else 1
                        holder.execute("SELECT guild_id FROM guild_wallet WHERE guild_id=%s FOR UPDATE", (second,))
                        if scenario == "deadlock":
                            report["first_retry"] = wait_for(
                                lambda: retry_logged(log, "40P01"), "데드락 재시도를 확인하지 못했습니다.")
                            if observer.execute("SELECT count(*) FROM settlement_result").fetchone()[0] != 0:
                                raise RuntimeError("데드락 롤백 후 결과가 남았습니다.")
                            if any(value != 0 for (value,) in observer.execute("SELECT balance FROM guild_wallet").fetchall()):
                                raise RuntimeError("데드락 롤백 후 잔액 변경이 남았습니다.")
                            report["rollback_after_deadlock"] = True
                child.wait(timeout=30)
                if child.returncode != 0:
                    raise RuntimeError("정산 작업자가 실패했습니다: " + log.read_text(encoding="utf-8"))
            records = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("settle-*.json")]
            if len(records) != 1:
                raise RuntimeError("정산 실행 기록 수가 예상과 다릅니다.")
            record = records[0]
            wanted = {"timeout": "55P03", "deadlock": "40P01", "ordered": None}[scenario]
            if wanted is None:
                if record["retries"] != 0:
                    raise RuntimeError("같은 순서의 잠금에서 예상 밖의 재시도가 발생했습니다.")
            elif not any(event["sqlstate"] == wanted for event in record["retry_events"]):
                raise RuntimeError("예상한 SQLSTATE가 실행 기록에 없습니다.")
            with psycopg.connect(worker, autocommit=True) as check:
                verification = settlement.compare(check, expected)
            settlement.settle()
            with psycopg.connect(worker, autocommit=True) as check:
                if settlement.compare(check, expected) != verification:
                    raise RuntimeError("재실행 후 데이터가 변경됐습니다.")
            report.update(status="passed", worker_exit=child.returncode, settlement=record,
                          verification=verification, rerun_unchanged=True,
                          lock_timeout="2s", worker_deadlock_timeout="1s" if scenario != "timeout" else "200ms")
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            (folder / "summary.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
