"""긴 스냅샷 유지·종료 전후의 VACUUM과 실행계획 관측."""
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from runner.indexes import scans


QUERY = ("SELECT guild_id, SUM(score) FROM score_event_mvcc "
         "WHERE job_id=1 AND guild_id BETWEEN 1 AND 10 GROUP BY guild_id ORDER BY guild_id")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def capture(conn, folder, stage, expected, row_count):
    if conn.execute(QUERY).fetchall() != expected:
        raise RuntimeError(f"{stage}: 집계 결과가 예상값과 다릅니다.")
    plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + QUERY).fetchone()[0]
    write_json(folder / f"{stage}-plan.json", plan)
    node_scans = scans(plan[0]["Plan"])
    stats = conn.execute("SELECT n_live_tup, n_dead_tup, vacuum_count, autovacuum_count, "
                         "last_vacuum, last_autovacuum FROM pg_stat_user_tables "
                         "WHERE relname='score_event_mvcc'").fetchone()
    heap, indexes, total, pages, visible = conn.execute(
        "SELECT pg_relation_size(oid), pg_indexes_size(oid), pg_total_relation_size(oid), "
        "relpages, relallvisible FROM pg_class WHERE oid='score_event_mvcc'::regclass").fetchone()
    actual_count = conn.execute("SELECT count(*) FROM score_event_mvcc").fetchone()[0]
    if actual_count != row_count:
        raise RuntimeError(f"{stage}: 실제 행 수가 예상과 다릅니다.")
    return {"stage": stage, "actual_row_count": actual_count, "n_live_tup_estimate": stats[0], "n_dead_tup_estimate": stats[1],
            "vacuum_count": stats[2], "autovacuum_count": stats[3],
            "last_vacuum": stats[4], "last_autovacuum": stats[5],
            "heap_bytes": heap, "index_bytes": indexes, "total_bytes": total,
            "relpages": pages, "relallvisible": visible, "scans": node_scans,
            "heap_fetches": sum(node["Heap Fetches"] for node in node_scans if "Heap Fetches" in node)
                            if any("Heap Fetches" in node for node in node_scans) else None,
            "explain_execution_ms": plan[0]["Execution Time"], "verification": "passed"}


def experiment(rows):
    run_id = uuid4().hex
    database = f"vacuum_{run_id}"
    folder = Path("results") / f"vacuum-{run_id}"
    folder.mkdir(parents=True)
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(os.environ["DATABASE_URL"]), "dbname": database})
    originals = [(event, 1, (event - 1) % 10000 + 1, event % 1000) for event in range(1, rows + 1)]
    def expected(rounds):
        totals = {}
        for _, _, guild, score in originals:
            if guild <= 10:
                totals[guild] = totals.get(guild, 0) + score + rounds
        return sorted(totals.items())
    write_json(folder / "expected.json", {"initial": expected(0), "updated": expected(3)})
    report = {"status": "failed", "folder": str(folder), "rows": rows, "update_rounds": 3, "stages": []}
    notices = []
    def notice_handler(notice):
        notices.append({"message": notice.message_primary, "detail": notice.message_detail})
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup, \
                    psycopg.connect(worker, autocommit=True) as check:
                setup.add_notice_handler(notice_handler)
                setup.execute(Path("sql/schema.sql").read_text(encoding="utf-8"))
                setup.execute("CREATE TABLE score_event_mvcc (LIKE score_event INCLUDING ALL)")
                setup.execute("CREATE INDEX mvcc_job_guild ON score_event_mvcc (job_id, guild_id) INCLUDE (score)")
                setup.execute("GRANT SELECT ON score_event_mvcc TO lab_worker")
                # 실험 테이블에서만 자동 처리를 멈춰 수동 VACUUM의 영향 관측.
                setup.execute("ALTER TABLE score_event_mvcc SET (autovacuum_enabled=false)")
                report["environment"] = {"postgresql": setup.execute("SELECT version()").fetchone()[0],
                    "psycopg": psycopg.__version__, "autovacuum_global": setup.execute("SHOW autovacuum").fetchone()[0],
                    "table_autovacuum_disabled_during_experiment": True}
                with setup.cursor().copy("COPY score_event_mvcc FROM STDIN") as copy:
                    for row in originals:
                        copy.write_row(row)
                def vacuum(stage):
                    notices.clear()
                    setup.execute("VACUUM (VERBOSE, ANALYZE) score_event_mvcc")
                    write_json(folder / f"{stage}-vacuum.json", notices)
                vacuum("initial")
                report["stages"].append(capture(check, folder, "initial", expected(0), rows))
                with psycopg.connect(worker, autocommit=True, application_name="old-snapshot") as reader:
                    with reader.transaction():
                        reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        old_sum = reader.execute("SELECT SUM(score) FROM score_event_mvcc").fetchone()[0]
                        if old_sum != sum(row[3] for row in originals):
                            raise RuntimeError("처음 읽은 점수 합계가 예상값과 다릅니다.")
                        report["snapshot"] = reader.execute("SELECT pg_current_snapshot()::text").fetchone()[0]
                        with psycopg.connect(admin, autocommit=True) as writer:
                            for _ in range(3):
                                writer.execute("UPDATE score_event_mvcc SET score=score+1")
                            writer.execute("SELECT pg_stat_force_next_flush()")
                        report["reader_activity"] = setup.execute(
                            "SELECT state, backend_xmin::text, xact_start FROM pg_stat_activity WHERE pid=%s",
                            (reader.info.backend_pid,)).fetchone()
                        report["stages"].append(capture(check, folder, "updated_snapshot_held", expected(3), rows))
                        vacuum("snapshot_held")
                        report["stages"].append(capture(check, folder, "vacuum_snapshot_held", expected(3), rows))
                        if reader.execute("SELECT SUM(score) FROM score_event_mvcc").fetchone()[0] != old_sum:
                            raise RuntimeError("긴 조회의 스냅샷이 유지되지 않았습니다.")
                vacuum("snapshot_released")
                report["stages"].append(capture(check, folder, "vacuum_snapshot_released", expected(3), rows))
                # 실험 후 테이블 설정을 복원하고 전체 행의 점수도 확인.
                setup.execute("ALTER TABLE score_event_mvcc RESET (autovacuum_enabled)")
                report["restored_reloptions"] = setup.execute(
                    "SELECT reloptions FROM pg_class WHERE oid='score_event_mvcc'::regclass").fetchone()[0]
                actual = check.execute("SELECT event_id, job_id, guild_id, score FROM score_event_mvcc ORDER BY event_id").fetchall()
                if actual != [(event, job, guild, score + 3) for event, job, guild, score in originals]:
                    raise RuntimeError("갱신 후 전체 행이 예상값과 다릅니다.")
                held, released = report["stages"][2:4]
                if held["n_dead_tup_estimate"] <= 0 or released["n_dead_tup_estimate"] != 0:
                    raise RuntimeError("스냅샷 유지·종료 후 VACUUM 통계가 예상과 다릅니다.")
                report.update(status="passed", snapshot_unchanged=True, all_rows_verified=True)
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            # 실패하더라도 실험 DB에서만 변경한 autovacuum 설정 복원 후 정리.
            with psycopg.connect(admin, autocommit=True) as cleanup:
                cleanup.execute("ALTER TABLE IF EXISTS score_event_mvcc RESET (autovacuum_enabled)")
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            write_json(folder / "summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
