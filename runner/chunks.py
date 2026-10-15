"""같은 입력에서 청크 크기만 바꿔 정산 비용을 비교."""
import csv
import json
import math
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from runner import settlement


SIZES = (10, 100, 1000, 10000)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def probe_wallet(url, ready, locked, cancelled):
    with psycopg.connect(url, autocommit=True, application_name="chunk-probe") as conn:
        conn.execute("SET lock_timeout='30s'")
        ready.set()
        while not locked.wait(0.02):
            if cancelled.is_set():
                return None
        start = time.perf_counter()
        with conn.transaction():
            conn.execute("SELECT guild_id FROM guild_wallet WHERE guild_id=1 FOR UPDATE")
        return (time.perf_counter() - start) * 1000


def observe(admin, stop, samples, errors):
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            while not stop.is_set():
                waits = conn.execute("SELECT pid, application_name, wait_event, pg_blocking_pids(pid) "
                                     "FROM pg_stat_activity WHERE datname=current_database() "
                                     "AND application_name IN ('chunk-worker', 'chunk-probe') "
                                     "AND wait_event_type='Lock'").fetchall()
                samples.append({"time": time.perf_counter(), "waits": waits})
                stop.wait(0.02)
    except Exception as exc:
        errors.append(str(exc))


def run_case(admin, worker, folder, expected, size, repetition):
    guilds = expected["guilds"]
    chunks = [(1, number, first, min(first + size - 1, guilds))
              for number, first in enumerate(range(1, guilds + 1, size), 1)]
    with psycopg.connect(admin, autocommit=True) as setup:
        with setup.transaction():
            setup.execute("TRUNCATE settlement_result, settlement_chunk, guild_wallet")
            with setup.cursor().copy("COPY guild_wallet (guild_id) FROM STDIN") as copy:
                for guild in range(1, guilds + 1):
                    copy.write_row((guild,))
            with setup.cursor().copy("COPY settlement_chunk "
                                     "(job_id, chunk_no, first_guild_id, last_guild_id) FROM STDIN") as copy:
                for chunk in chunks:
                    copy.write_row(chunk)
            setup.execute("UPDATE settlement_job SET state='READY', completed_at=NULL WHERE job_id=1")
        setup.execute("ANALYZE guild_wallet")
        setup.execute("ANALYZE settlement_chunk")
    expected = {**expected, "chunk_size": size, "chunks": chunks}
    ready, locked, stop = threading.Event(), threading.Event(), threading.Event()
    samples, errors, timings = [], [], []
    monitor = threading.Thread(target=observe, args=(admin, stop, samples, errors), daemon=True)
    monitor.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        probe = pool.submit(probe_wallet, worker, ready, locked, stop)
        try:
            if not ready.wait(10):
                raise RuntimeError("잠금 확인 연결이 준비되지 않았습니다.")
            with psycopg.connect(worker, autocommit=True, application_name="chunk-worker") as conn:
                start = time.perf_counter()
                for _, number, _, _ in chunks:
                    chunk_start = time.perf_counter()
                    processed = settlement.settle_chunk(conn, number, on_first_wallet=locked.set if number == 1 else None)
                    if not processed:
                        raise RuntimeError("초기화한 청크를 건너뛰었습니다.")
                    timings.append({"chunk_no": number, "elapsed_ms": (time.perf_counter() - chunk_start) * 1000})
                conn.execute("UPDATE settlement_job SET state='COMPLETED', completed_at=clock_timestamp() "
                             "WHERE job_id=1 AND NOT EXISTS(SELECT 1 FROM settlement_chunk "
                             "WHERE job_id=1 AND completed_at IS NULL)")
                end = time.perf_counter()
                probe_ms = probe.result(timeout=35)
        finally:
            stop.set()
            monitor.join(timeout=10)
    if monitor.is_alive() or errors:
        raise RuntimeError("잠금 관측 실패: " + "; ".join(errors))
    with psycopg.connect(worker, autocommit=True) as check:
        verification = settlement.compare(check, expected)
    prefix = f"size-{size}-repeat-{repetition}"
    with (folder / f"{prefix}-chunks.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["chunk_no", "elapsed_ms"])
        writer.writeheader()
        writer.writerows(timings)
    samples = [sample for sample in samples if start <= sample["time"] <= end]
    write_json(folder / f"{prefix}-waits.json", samples)
    latencies = [row["elapsed_ms"] for row in timings]
    return {"size": size, "repetition": repetition, "chunks": len(chunks), "elapsed_seconds": end - start,
            "guilds_per_second": guilds / (end - start), "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95), "p99_ms": percentile(latencies, 0.99),
            "probe_wait_ms": probe_ms, "lock_samples": len(samples),
            "samples_with_waits": sum(bool(sample["waits"]) for sample in samples),
            "verification": verification}


def experiment(guilds, events):
    database = f"chunks_{uuid4().hex}"
    folder = Path("results") / database.replace("_", "-", 1)
    folder.mkdir(parents=True)
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(os.environ["DATABASE_URL"]), "dbname": database})
    report = {"status": "failed", "folder": str(folder), "guilds": guilds, "events": events,
              "sizes": list(SIZES), "seed": 42, "warmups_per_size": 1, "repetitions": 5, "runs": []}
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup:
                setup.execute(Path("sql/schema.sql").read_text(encoding="utf-8"))
            os.environ["ADMIN_DATABASE_URL"] = admin
            os.environ["DATABASE_URL"] = worker
            settlement.EXPECTED = folder / "expected.json"
            settlement.prepare(SimpleNamespace(seed=42, guilds=guilds, events=events, chunk_size=10))
            expected = json.loads(settlement.EXPECTED.read_text(encoding="utf-8"))
            with psycopg.connect(admin, autocommit=True) as setup:
                # 모든 크기에 같은 커버링 인덱스와 VACUUM 조건 적용.
                setup.execute("CREATE INDEX score_event_job_guild ON score_event (job_id, guild_id) INCLUDE (score)")
                setup.execute("VACUUM (ANALYZE) score_event")
                report["environment"] = {"postgresql": setup.execute("SELECT version()").fetchone()[0],
                    "psycopg": psycopg.__version__, "index": "(job_id, guild_id) INCLUDE (score)",
                    "db_settings": dict(setup.execute("SELECT name, setting FROM pg_settings WHERE name IN "
                        "('shared_buffers', 'work_mem', 'synchronous_commit', 'fsync')").fetchall())}
            for repetition in range(6):
                order = list(SIZES[repetition % 4:] + SIZES[:repetition % 4])
                if repetition % 2:
                    order.reverse()
                for size in order:
                    result = run_case(admin, worker, folder, expected, size, repetition)
                    report["runs"].append(result)
                    write_json(folder / "progress.json", report)
                    print(json.dumps(result), flush=True)
            hashes = {(run["verification"]["result_hash"], run["verification"]["wallet_hash"])
                      for run in report["runs"]}
            if len(hashes) != 1:
                raise RuntimeError("청크 크기별 결과 또는 잔액이 다릅니다.")
            report["medians"] = {}
            for size in SIZES:
                measured = [run for run in report["runs"] if run["size"] == size and run["repetition"] > 0]
                report["medians"][size] = {key: statistics.median(run[key] for run in measured)
                    for key in ("elapsed_seconds", "guilds_per_second", "p50_ms", "p95_ms", "p99_ms", "probe_wait_ms")}
            report["status"] = "passed"
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            write_json(folder / "summary.json", report)
    print(json.dumps({"status": report["status"], "folder": str(folder), "medians": report["medians"]}, indent=2))
