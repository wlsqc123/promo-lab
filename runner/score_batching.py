"""40명의 즉시 갱신과 DB에 증가분을 남기는 지연 갱신 비교."""
import csv
import json
import os
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from runner.score_updates import USERS, apply_score, percentile, write_json
from runner.settlement import digest


INTERVAL = 0.05


def enqueue(conn, request_id, user, delta):
    with conn.transaction():
        row = conn.execute("UPDATE user_score SET score=score+%s WHERE user_id=%s RETURNING guild_id",
                           (delta, user)).fetchone()
        if row is None:
            raise ValueError("점수를 기록할 유저가 없습니다.")
        conn.execute("INSERT INTO pending_score VALUES (%s, %s, %s)", (request_id, row[0], delta))


def flush(conn, fail_before_commit=False):
    # 작업자 하나만 사용. 선택한 내역의 합산과 삭제는 같은 트랜잭션으로 처리.
    with conn.transaction():
        rows = conn.execute("SELECT request_id, guild_id, delta FROM pending_score "
                            "ORDER BY request_id LIMIT 10000 FOR UPDATE").fetchall()
        totals = {}
        for _, guild, delta in rows:
            totals[guild] = totals.get(guild, 0) + delta
        for guild, delta in sorted(totals.items()):
            if conn.execute("UPDATE guild_score SET score=score+%s WHERE guild_id=%s",
                            (delta, guild)).rowcount != 1:
                raise ValueError("점수를 기록할 길드가 없습니다.")
        if rows:
            conn.execute("DELETE FROM pending_score WHERE request_id=ANY(%s)", ([row[0] for row in rows],))
        if fail_before_commit:
            raise ValueError("커밋 전 실패 주입")
    return [row[0] for row in rows], len(totals)


def produce(url, user, deltas, case, gate):
    records = []
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            gate.wait(timeout=30)
            for sequence, delta in enumerate(deltas, 1):
                request_id = user * 100000 + sequence
                start = time.perf_counter()
                if case == "immediate":
                    apply_score(conn, user, 1, delta)
                else:
                    enqueue(conn, request_id, user, delta)
                ack = time.perf_counter()
                records.append({"request_id": request_id, "start": start, "ack": ack,
                                "request_ms": (ack - start) * 1000})
    except Exception:
        gate.abort()
        raise
    return records


def consume(url, gate, done, abort):
    reflected, batches = {}, []
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            gate.wait(timeout=30)
            while not abort.wait(INTERVAL):
                # 조회 전에 완료 여부를 읽어 마지막 쓰기와 종료 판단의 경쟁 방지.
                producers_done = done.is_set()
                ids, writes = flush(conn)
                observed = time.perf_counter()
                reflected.update((request_id, observed) for request_id in ids)
                if ids:
                    batches.append({"requests": len(ids), "guild_updates": writes})
                elif producers_done:
                    return reflected, batches, observed
        raise RuntimeError("합산 처리를 중단했습니다.")
    except Exception:
        gate.abort()
        raise


def run_case(admin, worker, folder, case, repetition, operations):
    expected_users = [(user, 1, sum(deltas)) for user, deltas in operations.items()]
    expected_guilds = [(1, sum(row[2] for row in expected_users))]
    with psycopg.connect(admin, autocommit=True) as setup:
        with setup.transaction():
            setup.execute("TRUNCATE user_score, guild_score, pending_score")
            setup.execute("INSERT INTO guild_score VALUES (1, 0)")
            setup.cursor().executemany("INSERT INTO user_score VALUES (%s, 1, 0)",
                                       [(user,) for user in operations])
    started = []
    gate = threading.Barrier(USERS + 1 + (case == "batched"),
                             action=lambda: started.append(time.perf_counter()))
    done, abort = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=USERS + 1) as pool:
        consumer = pool.submit(consume, worker, gate, done, abort) if case == "batched" else None
        futures = [pool.submit(produce, worker, user, deltas, case, gate)
                   for user, deltas in operations.items()]
        try:
            gate.wait(timeout=30)
            records = [row for future in futures for row in future.result()]
            acknowledged = max(row["ack"] for row in records)
            done.set()
            if consumer:
                reflected, batches, _ = consumer.result(timeout=60)
                if set(reflected) != {row["request_id"] for row in records}:
                    raise RuntimeError("합산한 요청 ID가 입력과 다릅니다.")
            else:
                reflected = {row["request_id"]: row["ack"] for row in records}
                batches = [{"requests": 1, "guild_updates": 1} for _ in records]
        finally:
            done.set()
            abort.set()
    with psycopg.connect(worker, autocommit=True) as check:
        users = check.execute("SELECT user_id, guild_id, score FROM user_score ORDER BY user_id").fetchall()
        guilds = check.execute("SELECT guild_id, score FROM guild_score ORDER BY guild_id").fetchall()
        pending = check.execute("SELECT count(*) FROM pending_score").fetchone()[0]
    if users != expected_users or guilds != expected_guilds or pending:
        raise RuntimeError("유저·길드 점수 또는 미반영 내역이 예상과 다릅니다.")
    for row in records:
        row["request_to_reflected_ms"] = (reflected[row["request_id"]] - row["start"]) * 1000
        row["ack_to_reflected_ms"] = max(0, reflected[row["request_id"]] - row["ack"]) * 1000
    with (folder / f"{case}-{repetition}-requests.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    write_json(folder / f"{case}-{repetition}-batches.json", batches)
    elapsed = acknowledged - started[0]
    result = {"case": case, "repetition": repetition, "requests": len(records),
              "ack_seconds": elapsed, "all_reflected_seconds": max(reflected.values()) - started[0],
              "requests_per_second": len(records) / elapsed,
              "guild_updates": sum(batch["guild_updates"] for batch in batches),
              "user_hash": digest(users), "guild_hash": digest(guilds), "verification": "passed"}
    for field in ("request_ms", "request_to_reflected_ms", "ack_to_reflected_ms"):
        for label, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
            result[f"{field}_{label}"] = percentile([row[field] for row in records], fraction)
    return result


def recovery_check(admin, worker):
    with psycopg.connect(admin, autocommit=True) as setup:
        setup.execute("TRUNCATE user_score, guild_score, pending_score")
        setup.execute("INSERT INTO guild_score VALUES (1, 0)")
        setup.execute("INSERT INTO user_score VALUES (1, 1, 0)")
    with psycopg.connect(worker, autocommit=True) as conn:
        enqueue(conn, 1, 1, 7)
        try:
            enqueue(conn, 1, 1, 7)
        except psycopg.errors.UniqueViolation:
            pass
        else:
            raise RuntimeError("중복 요청 ID가 거부되지 않았습니다.")
        if conn.execute("SELECT score FROM user_score").fetchone()[0] != 7:
            raise RuntimeError("요청 중복 실패 후 유저 점수가 롤백되지 않았습니다.")
        try:
            flush(conn, fail_before_commit=True)
        except ValueError:
            pass
        else:
            raise RuntimeError("합산 실패가 발생하지 않았습니다.")
        if (conn.execute("SELECT score FROM guild_score").fetchone()[0] != 0
                or conn.execute("SELECT count(*) FROM pending_score").fetchone()[0] != 1):
            raise RuntimeError("합산 실패 후 길드 점수와 내역이 롤백되지 않았습니다.")
    # 새 연결에서 미반영 내역을 재처리하고 다시 실행해 중복 반영 여부 확인.
    with psycopg.connect(worker, autocommit=True) as conn:
        if flush(conn)[0] != [1] or flush(conn)[0]:
            raise RuntimeError("재연결 후 미반영 내역 처리 결과가 다릅니다.")
        if conn.execute("SELECT score FROM guild_score").fetchone()[0] != 7:
            raise RuntimeError("재실행 후 점수가 다릅니다.")
    return {"status": "passed", "duplicate_enqueue_rollback": "passed",
            "flush_rollback": "passed", "reconnect_and_replay": "passed",
            "second_flush_empty": "passed", "expected_user_score": 7, "expected_guild_score": 7}


def experiment(requests_per_user):
    run_id = uuid4().hex
    folder = Path("results") / f"score-batching-{run_id}"
    folder.mkdir(parents=True)
    database = f"score_batching_{run_id}"
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(os.environ["DATABASE_URL"]), "dbname": database})
    rng = random.Random(42)
    operations = {user: [rng.randint(1, 100) for _ in range(requests_per_user)]
                  for user in range(1, USERS + 1)}
    write_json(folder / "operations.json", operations)
    report = {"status": "failed", "folder": str(folder), "users": USERS, "guilds": 1, "seed": 42,
              "requests_per_user": requests_per_user, "interval_seconds": INTERVAL,
              "batch_limit": 10000, "warmups_per_case": 1, "repetitions": 5, "runs": []}
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup:
                setup.execute(Path("sql/score_updates.sql").read_text(encoding="utf-8"))
                setup.execute(Path("sql/score_batching.sql").read_text(encoding="utf-8"))
                report["environment"] = {"postgresql": setup.execute("SELECT version()").fetchone()[0],
                    "psycopg": psycopg.__version__, "db_settings": dict(setup.execute(
                    "SELECT name, setting FROM pg_settings WHERE name IN "
                    "('shared_buffers', 'fsync', 'synchronous_commit', 'max_connections')").fetchall())}
            report["recovery_check"] = recovery_check(admin, worker)
            for repetition in range(6):
                order = ["immediate", "batched"]
                if repetition % 2:
                    order.reverse()
                for case in order:
                    result = run_case(admin, worker, folder, case, repetition, operations)
                    report["runs"].append(result)
                    print(json.dumps(result), flush=True)
            if (len({run["user_hash"] for run in report["runs"]}) != 1
                    or len({run["guild_hash"] for run in report["runs"]}) != 1):
                raise RuntimeError("방식별·반복별 최종 점수가 다릅니다.")
            report["medians"] = {}
            for case in ("immediate", "batched"):
                measured = [run for run in report["runs"] if run["case"] == case and run["repetition"] > 0]
                report["medians"][case] = {key: statistics.median(run[key] for run in measured)
                    for key in ("ack_seconds", "all_reflected_seconds", "requests_per_second", "guild_updates",
                                "request_ms_p50", "request_ms_p95", "request_ms_p99",
                                "request_to_reflected_ms_p95", "ack_to_reflected_ms_p95")}
            report["status"] = "passed"
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            write_json(folder / "summary.json", report)
    print(json.dumps({"status": report["status"], "folder": str(folder), "medians": report["medians"]}, indent=2))
