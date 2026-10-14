"""같은 입력을 세 인덱스 구성에 적재하고 집계·적재 비용을 비교."""
import csv
import hashlib
import json
import os
import random
import statistics
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from runner.settlement import digest


CASES = ("baseline", "composite", "covering")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def scans(node):
    found = []
    if "Scan" in node["Node Type"]:
        found.append({key: node[key] for key in ("Node Type", "Index Name", "Actual Rows", "Actual Loops",
                                                "Plan Rows", "Rows Removed by Filter", "Heap Fetches") if key in node})
    for child in node.get("Plans", []):
        found.extend(scans(child))
    return found


def experiment(events):
    run_id = uuid4().hex
    database = f"indexes_{run_id}"
    folder = Path("results") / f"indexes-{run_id}"
    folder.mkdir(parents=True)
    rng = random.Random(42)
    totals = {}
    data_path = folder / "events.csv"
    with data_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        for event_id in range(1, events + 1):
            job, guild, score = rng.randint(1, 4), rng.randint(1, 10000), rng.randint(0, 1000)
            writer.writerow((event_id, job, guild, score))
            if job == 1 and guild <= 100:
                totals[guild] = totals.get(guild, 0) + score
    expected = sorted(totals.items())
    write_json(folder / "expected.json", expected)
    with data_path.open("rb") as file:
        input_hash = hashlib.file_digest(file, "sha256").hexdigest()
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(os.environ["DATABASE_URL"]), "dbname": database})
    report = {"status": "failed", "folder": str(folder), "events_per_case": events,
              "guilds": 10000, "jobs": 4, "seed": 42, "input_file_sha256": input_hash,
              "query_parameters": [1, 1, 100], "repetitions": 5, "warmups_per_case": 1,
              "cases": {}, "measurements": []}
    query_template = Path("sql/aggregate_scores.sql").read_text(encoding="utf-8")
    parameters = (1, 1, 100)
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup:
                setup.execute(Path("sql/schema.sql").read_text(encoding="utf-8"))
                report["environment"] = {"postgresql": setup.execute("SELECT version()").fetchone()[0],
                    "psycopg": psycopg.__version__, "db_settings": dict(setup.execute(
                        "SELECT name, setting FROM pg_settings WHERE name IN "
                        "('shared_buffers', 'work_mem', 'max_parallel_workers_per_gather', "
                        "'random_page_cost', 'effective_cache_size', 'synchronous_commit', 'fsync')").fetchall())}
                for case in CASES:
                    name = sql.Identifier(case)
                    table = sql.Identifier(case, "score_event")
                    setup.execute(sql.SQL("CREATE SCHEMA {}").format(name))
                    setup.execute(sql.SQL("CREATE TABLE {} (LIKE public.score_event INCLUDING ALL)").format(table))
                    if case == "composite":
                        setup.execute(sql.SQL("CREATE INDEX score_event_job_guild ON {} (job_id, guild_id)").format(table))
                    elif case == "covering":
                        setup.execute(sql.SQL("CREATE INDEX score_event_job_guild ON {} (job_id, guild_id) INCLUDE (score)").format(table))
                    setup.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO lab_worker").format(name))
                    setup.execute(sql.SQL("GRANT SELECT ON {} TO lab_worker").format(table))

                # 같은 파일을 각 테이블에 입력. 인덱스를 유지하는 비용과 COMMIT까지 포함.
                for case in CASES:
                    table = sql.Identifier(case, "score_event")
                    start = time.perf_counter()
                    with setup.transaction():
                        with setup.cursor().copy(sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV)").format(table)) as copy:
                            with data_path.open("rb") as file:
                                while block := file.read(65536):
                                    copy.write(block)
                    report["cases"][case] = {"load_seconds": time.perf_counter() - start}
                    if setup.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()[0] != events:
                        raise RuntimeError("적재한 이벤트 수가 예상과 다릅니다.")
                for case in CASES:
                    setup.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(case, "score_event")))
                    indexes = setup.execute("SELECT indexname, pg_relation_size(quote_ident(schemaname) || '.' || quote_ident(indexname)) "
                                            "FROM pg_indexes WHERE schemaname=%s AND tablename='score_event' "
                                            "ORDER BY indexname", (case,)).fetchall()
                    report["cases"][case]["indexes"] = indexes
                    report["cases"][case]["index_bytes"] = sum(size for _, size in indexes)

            with psycopg.connect(worker, autocommit=True, prepare_threshold=None) as conn:
                for repetition in range(6):
                    order = list(CASES[repetition % 3:] + CASES[:repetition % 3])
                    if repetition % 2:
                        order.reverse()
                    for case in order:
                        query = sql.SQL(query_template).format(schema=sql.Identifier(case))
                        start = time.perf_counter()
                        rows = conn.execute(query, parameters).fetchall()
                        elapsed_ms = (time.perf_counter() - start) * 1000
                        if rows != expected:
                            raise RuntimeError(f"{case} 집계 결과가 별도 예상값과 다릅니다.")
                        report["measurements"].append({"case": case, "repetition": repetition,
                            "client_ms": elapsed_ms, "rows": len(rows), "result_hash": digest(rows)})
                # EXPLAIN 계측은 처리시간 측정과 분리.
                for case in CASES:
                    query = sql.SQL(query_template).format(schema=sql.Identifier(case))
                    plan = conn.execute(sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ") + query, parameters).fetchone()[0]
                    write_json(folder / f"{case}-plan.json", plan)
                    root = plan[0]["Plan"]
                    measured = [row["client_ms"] for row in report["measurements"]
                                if row["case"] == case and row["repetition"] > 0]
                    report["cases"][case].update(client_median_ms=statistics.median(measured),
                        client_min_ms=min(measured), client_max_ms=max(measured),
                        explain_execution_ms=plan[0]["Execution Time"], plan_rows=root["Plan Rows"],
                        actual_rows=root["Actual Rows"], shared_hit_blocks=root.get("Shared Hit Blocks", 0),
                        shared_read_blocks=root.get("Shared Read Blocks", 0), scans=scans(root))
            report["status"] = "passed"
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            write_json(folder / "summary.json", report)
    with (folder / "measurements.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(report["measurements"][0]))
        writer.writeheader()
        writer.writerows(report["measurements"])
    print(json.dumps({"status": report["status"], "folder": str(folder), "cases": report["cases"]}, indent=2))
