"""논리 백업 전후의 데이터·정의·권한과 복원 DB의 재실행 확인."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from runner import settlement


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def snapshot(url, expected):
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        verification = settlement.compare(conn, expected)
        tables = {}
        for table in settlement.TABLES:
            rows = conn.execute(sql.SQL("SELECT * FROM {} ORDER BY 1, 2").format(sql.Identifier(table))).fetchall()
            tables[table] = {"rows": len(rows), "hash": settlement.digest(rows)}
        definitions = {}
        queries = {
            "columns": "SELECT c.relname, a.attnum, a.attname, format_type(a.atttypid,a.atttypmod), "
                "a.attnotnull, pg_get_expr(d.adbin,d.adrelid), a.attidentity, a.attgenerated "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid=c.oid "
                "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
                "WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped "
                "ORDER BY c.relname,a.attnum",
            "constraints": "SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid) "
                "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
                "ORDER BY c.relname,k.conname",
            "indexes": "SELECT tablename,indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='public' ORDER BY tablename,indexname",
            "owners": "SELECT c.relname,pg_get_userbyid(c.relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
                "AND c.relkind='r' ORDER BY c.relname",
            "table_acl": "SELECT c.relname,x.grantor::regrole::text,x.grantee::regrole::text, "
                "x.privilege_type,x.is_grantable FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) x "
                "WHERE n.nspname='public' AND c.relkind='r' ORDER BY 1,2,3,4",
            "column_acl": "SELECT c.relname,a.attname,x.grantor::regrole::text,x.grantee::regrole::text, "
                "x.privilege_type,x.is_grantable FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid "
                "CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE n.nspname='public' ORDER BY 1,2,3,4,5",
            "schema_acl": "SELECT n.nspname,pg_get_userbyid(n.nspowner),x.grantor::regrole::text, "
                "x.grantee::regrole::text,x.privilege_type,x.is_grantable FROM pg_namespace n "
                "CROSS JOIN LATERAL aclexplode(n.nspacl) x WHERE n.nspname='public' ORDER BY 1,3,4,5",
            "extensions": "SELECT extname,extversion FROM pg_extension ORDER BY extname",
        }
        for name, query in queries.items():
            definitions[name] = conn.execute(query).fetchall()
        return {"verification": verification, "tables": tables,
                "schema_hash": settlement.digest(definitions), "definitions": definitions}


def check(folder, database):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError("기록 폴더가 없습니다.")
    source = os.environ["DATABASE_URL"]
    if database is None:
        expected = json.loads(settlement.EXPECTED.read_text(encoding="utf-8"))
        write_json(folder / "expected.json", expected)
        write_json(folder / "source-snapshot.json", snapshot(source, expected))
        print(json.dumps({"status": "passed", "phase": "before-backup"}))
        return
    if not re.fullmatch(r"restore_[0-9a-f]{32}", database):
        raise ValueError("이 실험에서 만든 복원 DB 이름만 사용할 수 있습니다.")
    restored = make_conninfo(**{**conninfo_to_dict(source), "dbname": database})
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    before = json.loads((folder / "source-snapshot.json").read_text(encoding="utf-8"))
    current, recovered = snapshot(source, expected), snapshot(restored, expected)
    # 파일의 JSON 배열과 DB의 tuple 표현을 맞춰 전체 정의까지 비교.
    normalized = lambda value: json.loads(json.dumps(value, default=str))
    if normalized(current) != before or normalized(recovered) != before:
        raise RuntimeError("백업 전·현재 원본·복원 DB의 데이터 또는 정의·권한이 다릅니다.")
    with psycopg.connect(restored, autocommit=True) as conn:
        for query in ("UPDATE guild_wallet SET balance=balance WHERE false",
                      "UPDATE settlement_chunk SET completed_at=completed_at WHERE false",
                      "INSERT INTO settlement_result SELECT * FROM settlement_result WHERE false"):
            with conn.transaction():
                conn.execute(query)
                raise psycopg.Rollback()
        for query in ("UPDATE score_event SET score=score WHERE false",
                      "DELETE FROM settlement_result WHERE false",
                      "UPDATE settlement_chunk SET first_guild_id=first_guild_id WHERE false"):
            try:
                with conn.transaction():
                    conn.execute(query)
                    raise RuntimeError("금지한 변경이 복원 DB에서 허용됐습니다.")
            except psycopg.errors.InsufficientPrivilege:
                pass
    env = {**os.environ, "DATABASE_URL": restored, "RESULTS_DIR": str(folder)}
    result = subprocess.run([sys.executable, "-m", "runner", "settle"], env=env,
                            capture_output=True, text=True, timeout=60)
    (folder / "restored-settle.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("복원 DB의 정산 재실행이 실패했습니다.")
    rerun = json.loads(result.stdout)
    if rerun["processed_chunks"] != 0 or rerun["skipped_chunks"] != len(expected["chunks"]):
        raise RuntimeError("복원 DB에서 완료 청크를 다시 처리했습니다.")
    if normalized(snapshot(restored, expected)) != before or normalized(snapshot(source, expected)) != before:
        raise RuntimeError("재실행 후 원본 또는 복원 데이터가 변경됐습니다.")
    write_json(folder / "verification.json", {"status": "passed", "tables": recovered["tables"],
        "schema_hash": recovered["schema_hash"], "verification": recovered["verification"],
        "schema_and_acl_equal": True, "worker_allowed_checks": 3, "worker_denied_checks": 3,
        "rerun_processed_chunks": rerun["processed_chunks"], "rerun_skipped_chunks": rerun["skipped_chunks"],
        "rerun_unchanged": True, "source_unchanged": True})
    print(json.dumps({"status": "passed", "phase": "after-restore"}))
