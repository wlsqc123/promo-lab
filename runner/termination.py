"""작업자 강제 종료 후 롤백·재실행 확인. 별도 실험 DB 사용."""
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


def snapshot(conn):
    return {
        "results": conn.execute("SELECT * FROM settlement_result ORDER BY job_id, guild_id").fetchall(),
        "wallets": conn.execute("SELECT guild_id, balance FROM guild_wallet ORDER BY guild_id").fetchall(),
        "chunks": conn.execute("SELECT * FROM settlement_chunk ORDER BY job_id, chunk_no").fetchall(),
        "jobs": conn.execute("SELECT job_id, state, completed_at FROM settlement_job ORDER BY job_id").fetchall(),
    }


def experiment(point):
    run_id = uuid4().hex
    database = f"termination_{run_id}"
    folder = Path("results") / f"termination-{point}-{run_id}"
    folder.mkdir(parents=True)
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(os.environ["DATABASE_URL"]), "dbname": database})
    marker = folder / "pause.json"
    child = None
    report = {"point": point, "status": "failed", "database": database, "folder": str(folder)}
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup:
                setup.execute(Path("sql/schema.sql").read_text(encoding="utf-8"))
            os.environ["ADMIN_DATABASE_URL"] = admin
            os.environ["DATABASE_URL"] = worker
            settlement.EXPECTED = folder / "expected.json"
            settlement.prepare(SimpleNamespace(seed=42, guilds=100, events=10000, chunk_size=10))
            expected = json.loads(settlement.EXPECTED.read_text(encoding="utf-8"))
            env = {**os.environ, "RESULTS_DIR": str(folder), "WORKER_NAME": "termination-worker",
                   "CRASH_POINT": point, "CRASH_MARKER": str(marker)}
            child = subprocess.Popen([sys.executable, "-m", "runner", "settle"], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 10
            while not marker.exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("작업자가 지정한 종료 지점에 도달하지 못했습니다.")
                time.sleep(0.02)
            # 파일 쓰기가 끝난 뒤 DB 세션도 확인한 상태에서 작업자만 종료.
            pause = json.loads(marker.read_text(encoding="utf-8"))
            with psycopg.connect(admin, autocommit=True) as observer:
                activity = observer.execute("SELECT state, xact_start IS NOT NULL "
                                            "FROM pg_stat_activity WHERE pid=%s AND datname=%s",
                                            (pause["backend_pid"], database)).fetchone()
                if activity is None or activity[1] != (point == "before-commit"):
                    raise RuntimeError("종료 지점의 트랜잭션 상태가 예상과 다릅니다.")
                report["paused_session"] = activity
                child.kill()
                stdout, stderr = child.communicate(timeout=10)
                (folder / "terminated-worker.log").write_text(stdout + stderr, encoding="utf-8")
                report["worker_exit"] = child.returncode
                if child.returncode != -9:
                    raise RuntimeError("작업자가 SIGKILL로 종료되지 않았습니다.")
                deadline = time.monotonic() + 10
                while observer.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                       "WHERE pid=%s AND datname=%s)",
                                       (pause["backend_pid"], database)).fetchone()[0]:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("종료한 작업자의 DB 연결이 남아 있습니다.")
                    time.sleep(0.02)
                before = snapshot(observer)
                committed = 10 if point == "after-commit" else 0
                wanted_results = [tuple(row) for row in expected["results"][:committed]]
                wanted_wallets = [(row[1], row[3] if i < committed else 0)
                                  for i, row in enumerate(expected["results"])]
                if [row[:4] for row in before["results"]] != wanted_results:
                    raise RuntimeError("종료 후 결과가 롤백·커밋 예상과 다릅니다.")
                if before["wallets"] != wanted_wallets:
                    raise RuntimeError("종료 후 잔액이 롤백·커밋 예상과 다릅니다.")
                if [row[-1] is not None for row in before["chunks"]] != [point == "after-commit"] + [False] * 9:
                    raise RuntimeError("종료 후 청크 완료 기록이 예상과 다릅니다.")
                if before["jobs"] != [(1, "READY", None)]:
                    raise RuntimeError("다음 청크 처리 전 정산 상태가 예상과 다릅니다.")
                (folder / "after-termination.json").write_text(
                    json.dumps(before, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            settlement.settle()
            with psycopg.connect(worker, autocommit=True) as check:
                verification = settlement.compare(check, expected)
                after = snapshot(check)
            if point == "after-commit":
                if before["results"] != after["results"][:10] or before["chunks"][0] != after["chunks"][0]:
                    raise RuntimeError("커밋한 첫 청크의 결과 또는 완료 시각이 변경됐습니다.")
            recovery = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("settle-*.json")]
            if (len(recovery) != 1 or recovery[0]["processed_chunks"] != 10 - committed // 10
                    or recovery[0]["skipped_chunks"] != committed // 10):
                raise RuntimeError("복구 실행의 처리 청크 수가 예상과 다릅니다.")
            settlement.settle()
            with psycopg.connect(worker, autocommit=True) as check:
                if settlement.compare(check, expected) != verification:
                    raise RuntimeError("추가 재실행 후 데이터가 변경됐습니다.")
            report.update(status="passed", results_after_termination=committed,
                          completed_chunks_after_termination=committed // 10,
                          recovery=recovery[0], verification=verification, rerun_unchanged=True)
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                stdout, stderr = child.communicate(timeout=10)
                if not (folder / "terminated-worker.log").exists():
                    (folder / "terminated-worker.log").write_text(stdout + stderr, encoding="utf-8")
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            (folder / "summary.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
