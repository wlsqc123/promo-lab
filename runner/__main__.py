import argparse
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from runner.settlement import prepare, settle, verify


def doctor():
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        version, zone, isolation, lock_timeout, statement_timeout, extension = conn.execute(
            "SELECT version(), current_setting('TimeZone'), "
            "current_setting('transaction_isolation'), current_setting('lock_timeout'), "
            "current_setting('statement_timeout'), "
            "EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements')"
        ).fetchone()
        assert conn.info.server_version // 10000 == 18, version
        assert zone == "UTC", zone
        assert isolation == "read committed", isolation
        assert extension, "pg_stat_statements missing"
        conn.execute("SELECT count(*) FROM pg_stat_statements").fetchone()
        with conn.transaction():
            conn.execute("CREATE TEMP TABLE connection_probe (value bigint NOT NULL)")
            conn.execute("INSERT INTO connection_probe VALUES (42)")
            assert conn.execute("SELECT value FROM connection_probe").fetchone() == (42,)
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "postgresql": version, "python": platform.python_version(),
            "psycopg": psycopg.__version__, "timezone": zone,
            "isolation": isolation, "lock_timeout": lock_timeout,
            "statement_timeout": statement_timeout,
            "pg_stat_statements": extension, "connection_probe": "passed",
        }
    Path("results").mkdir(exist_ok=True)
    Path("results/environment.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


parser = argparse.ArgumentParser(description="PostgreSQL 정산 실험 CLI")
commands = parser.add_subparsers(dest="command", required=True)
commands.add_parser("doctor")
data = commands.add_parser("prepare", help="빈 DB에 실험 데이터와 예상값 준비")
data.add_argument("--seed", type=int, default=42)
data.add_argument("--guilds", type=int, default=100)
data.add_argument("--events", type=int, default=10000)
data.add_argument("--chunk-size", type=int, default=10)
commands.add_parser("settle", help="job 1 정산")
commands.add_parser("verify", help="완료된 정산을 예상값과 비교")
duplicate = commands.add_parser("duplicate", help="별도 DB에서 같은 정산을 동시에 실행")
duplicate.add_argument("--workers", type=int, choices=[2, 4, 8], default=2)
termination = commands.add_parser("terminate", help="작업자 강제 종료 후 재실행 확인")
termination.add_argument("--point", choices=["before-commit", "after-commit"], required=True)
locking = commands.add_parser("locking", help="락 시간 초과·데드락·같은 순서 잠금 확인")
locking.add_argument("--scenario", choices=["timeout", "deadlock", "ordered"], required=True)
scores = commands.add_parser("score-updates", help="40개 연결의 유저·길드 점수 갱신 비교")
scores.add_argument("--requests-per-user", type=int, default=50)
args = parser.parse_args()
if args.command == "score-updates" and not 1 <= args.requests_per_user <= 1000:
    parser.error("유저당 요청 수는 1~1000을 사용하세요.")
if args.command == "prepare":
    if not (2 <= args.guilds <= 10000 and 0 <= args.events <= 1000000
            and 1 <= args.chunk_size <= args.guilds):
        parser.error("길드 2~10000, 이벤트 0~1000000, 청크 크기 1~길드 수를 사용하세요.")
try:
    if args.command == "doctor":
        doctor()
    elif args.command == "prepare":
        prepare(args)
    elif args.command == "settle":
        settle()
    elif args.command == "verify":
        verify()
    elif args.command == "duplicate":
        from runner.concurrent import experiment
        experiment(args.workers)
    elif args.command == "terminate":
        from runner.termination import experiment
        experiment(args.point)
    elif args.command == "locking":
        from runner.locking import experiment
        experiment(args.scenario)
    else:
        from runner.score_updates import experiment
        experiment(args.requests_per_user)
except (psycopg.Error, ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
    print(json.dumps({"status": "failed", "error": str(exc),
                      "sqlstate": getattr(exc, "sqlstate", None)}, ensure_ascii=False))
    raise SystemExit(1)
