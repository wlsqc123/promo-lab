"""같은 정산의 중복 실행 실험. 기존 DB 대신 별도 실험 DB 사용."""
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


def experiment(workers):
    run_id = uuid4().hex
    database = f"duplicate_{run_id}"
    folder = Path("results") / f"duplicate-{workers}-{run_id}"
    folder.mkdir(parents=True)
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    worker_url = os.environ["DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(worker_url), "dbname": database})
    children = []
    outputs = []
    report = {"workers": workers, "database": database, "status": "failed",
              "purpose": "같은 정산의 중복 실행", "folder": str(folder)}
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup:
                setup.execute(Path("sql/schema.sql").read_text(encoding="utf-8"))
            os.environ["ADMIN_DATABASE_URL"] = admin
            os.environ["DATABASE_URL"] = worker
            settlement.EXPECTED = folder / "expected.json"
            settlement.prepare(SimpleNamespace(seed=42, guilds=100, events=10000, chunk_size=10))

            with psycopg.connect(admin, autocommit=True) as observer:
                # 첫 지갑을 잡아 첫 작업자가 청크 잠금을 유지하게 하고, 다른 작업자의 대기를 관측.
                with observer.transaction():
                    observer.execute("SELECT guild_id FROM guild_wallet WHERE guild_id=1 FOR UPDATE")
                    for number in range(1, workers + 1):
                        env = {**os.environ, "RESULTS_DIR": str(folder),
                               "WORKER_NAME": f"duplicate-worker-{number}"}
                        children.append(subprocess.Popen(
                            [sys.executable, "-m", "runner", "settle"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        # 관측 연결의 트랜잭션 안에서도 작업자 상태를 새로 조회.
                        observer.execute("SELECT pg_stat_clear_snapshot()")
                        activity = observer.execute(
                            "SELECT pid, application_name, wait_event_type, wait_event, "
                            "pg_blocking_pids(pid) FROM pg_stat_activity "
                            "WHERE datname=%s AND application_name LIKE 'duplicate-worker-%%'",
                            (database,)).fetchall()
                        worker_pids = {row[0] for row in activity}
                        if (len(activity) == workers and all(row[2] == "Lock" for row in activity)
                                and any(worker_pids.intersection(row[4]) for row in activity)):
                            report["lock_observation"] = activity
                            break
                        time.sleep(0.02)
                    else:
                        raise RuntimeError("작업자 사이의 잠금 대기를 관측하지 못했습니다.")

            for number, child in enumerate(children, 1):
                stdout, stderr = child.communicate(timeout=30)
                (folder / f"worker-{number}.log").write_text(stdout + stderr, encoding="utf-8")
                outputs.append({"worker": number, "exit_code": child.returncode})
                if child.returncode != 0:
                    raise RuntimeError(f"작업자 {number} 실패: {stdout} {stderr}")
            report["worker_exits"] = outputs
            with psycopg.connect(worker, autocommit=True) as check:
                before = settlement.compare(check, json.loads(settlement.EXPECTED.read_text(encoding="utf-8")))
            settlements = [json.loads(path.read_text(encoding="utf-8"))
                           for path in folder.glob("settle-*.json")]
            processed = sum(item["processed_chunks"] for item in settlements)
            skipped = sum(item["skipped_chunks"] for item in settlements)
            if len(settlements) != workers or processed != 10 or skipped != 10 * (workers - 1):
                raise RuntimeError("처리·건너뛰기 청크 수가 예상과 다릅니다.")
            settlement.settle()
            with psycopg.connect(worker, autocommit=True) as check:
                after = settlement.compare(check, json.loads(settlement.EXPECTED.read_text(encoding="utf-8")))
            if before != after:
                raise RuntimeError("재실행 후 결과·잔액·완료 기록이 변경됐습니다.")
            report.update(status="passed", processed_chunks=processed, skipped_chunks=skipped,
                          verification=before, rerun_unchanged=True,
                          retries=sum(item["retries"] for item in settlements))
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            for number, child in enumerate(children, 1):
                if not (folder / f"worker-{number}.log").exists():
                    stdout, stderr = child.communicate()
                    (folder / f"worker-{number}.log").write_text(stdout + stderr, encoding="utf-8")
            # 이 실행에서 만든 DB만 제거. 실패 시에도 파일 기록은 보존.
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            (folder / "summary.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
