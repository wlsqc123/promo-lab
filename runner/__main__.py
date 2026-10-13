import argparse
import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path

import psycopg


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
parser.add_argument("command", choices=["doctor"])
parser.parse_args()
doctor()
