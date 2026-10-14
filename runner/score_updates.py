"""유저별 점수와 길드 공용 점수를 같은 트랜잭션으로 누적하는 비교 실험."""
import csv
import json
import math
import os
import platform
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

from runner.settlement import digest


USERS = 40


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def apply_score(conn, user_id, guild_id, delta):
    with conn.transaction():
        if conn.execute("UPDATE user_score SET score=score+%s WHERE user_id=%s",
                        (delta, user_id)).rowcount != 1:
            raise ValueError("점수를 기록할 유저가 없습니다.")
        update_start = time.perf_counter()
        if conn.execute("UPDATE guild_score SET score=score+%s WHERE guild_id=%s",
                        (delta, guild_id)).rowcount != 1:
            raise ValueError("점수를 기록할 길드가 없습니다.")
        update_ms = (time.perf_counter() - update_start) * 1000
    return update_ms


def perform_updates(url, user_id, guild_id, deltas, gate):
    rows = []
    try:
        with psycopg.connect(url, autocommit=True, application_name=f"score-user-{user_id}") as conn:
            gate.wait(timeout=30)
            for sequence, delta in enumerate(deltas, 1):
                start = time.perf_counter()
                retry_codes = []
                update_ms = 0
                for attempt in range(4):
                    try:
                        update_ms += apply_score(conn, user_id, guild_id, delta)
                        break
                    except psycopg.Error as exc:
                        if exc.sqlstate not in ("40P01", "55P03", "40001") or attempt == 3:
                            raise
                        retry_codes.append(exc.sqlstate)
                        time.sleep(0.01 * 2 ** attempt + random.uniform(0, 0.01))
                rows.append({"user_id": user_id, "sequence": sequence, "guild_id": guild_id,
                             "delta": delta, "transaction_ms": (time.perf_counter() - start) * 1000,
                             "guild_update_ms": update_ms, "retries": len(retry_codes),
                             "sqlstates": ",".join(retry_codes)})
    except Exception:
        gate.abort()
        raise
    return rows


def observe(url, stop, observations, errors):
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            while not stop.is_set():
                waits = conn.execute(
                    "SELECT pid, application_name, wait_event, pg_blocking_pids(pid) "
                    "FROM pg_stat_activity WHERE datname=current_database() "
                    "AND application_name LIKE 'score-user-%%' AND wait_event_type='Lock'").fetchall()
                observations.append({"time": time.perf_counter(), "blocked": waits})
                stop.wait(0.02)
    except Exception as exc:
        errors.append(str(exc))


def run_case(admin, worker, folder, case, repetition, operations):
    guild_count = 1 if case == "concentrated" else 4
    assignments = {user: (user - 1) % guild_count + 1 for user in operations}
    expected_users = [(user, assignments[user], sum(deltas)) for user, deltas in operations.items()]
    expected_guilds = [(guild, sum(total for _, group, total in expected_users if group == guild))
                       for guild in range(1, guild_count + 1)]
    with psycopg.connect(admin, autocommit=True) as setup:
        with setup.transaction():
            setup.execute("TRUNCATE user_score, guild_score")
            setup.cursor().executemany("INSERT INTO guild_score (guild_id) VALUES (%s)",
                                       [(guild,) for guild in range(1, guild_count + 1)])
            setup.cursor().executemany("INSERT INTO user_score (user_id, guild_id) VALUES (%s, %s)",
                                       list(assignments.items()))
        setup.execute("ANALYZE user_score")
        setup.execute("ANALYZE guild_score")
    prefix = f"{case}-{repetition}"
    # 기대값은 DB 집계 SQL 대신 준비한 증가분으로 계산.
    write_json(folder / f"{prefix}-expected.json", {"users": expected_users, "guilds": expected_guilds})
    started = []
    gate = threading.Barrier(USERS + 1, action=lambda: started.append(time.perf_counter()))
    stop = threading.Event()
    observations, errors = [], []
    monitor = threading.Thread(target=observe, args=(admin, stop, observations, errors), daemon=True)
    monitor.start()
    try:
        with ThreadPoolExecutor(max_workers=USERS) as pool:
            futures = [pool.submit(perform_updates, worker, user, assignments[user], deltas, gate)
                       for user, deltas in operations.items()]
            # 연결 준비 시간을 처리시간에 포함하지 않도록 모든 연결이 준비된 뒤 시작.
            gate.wait(timeout=30)
            start = started[0]
            rows = [row for future in futures for row in future.result()]
            end = time.perf_counter()
            elapsed = end - start
    finally:
        stop.set()
        monitor.join(timeout=10)
    if monitor.is_alive() or errors:
        raise RuntimeError("잠금 관측 실패: " + "; ".join(errors))
    observations = [sample for sample in observations if start <= sample["time"] <= end]
    with psycopg.connect(worker, autocommit=True) as check:
        users = check.execute("SELECT user_id, guild_id, score FROM user_score ORDER BY user_id").fetchall()
        guilds = check.execute("SELECT guild_id, score FROM guild_score ORDER BY guild_id").fetchall()
    if users != expected_users or guilds != expected_guilds or len(rows) != sum(map(len, operations.values())):
        raise RuntimeError("유저별·길드별 점수 또는 처리 요청 수가 예상값과 다릅니다.")
    with (folder / f"{prefix}-requests.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write_json(folder / f"{prefix}-waits.json", observations)
    latencies = [row["transaction_ms"] for row in rows]
    update_latencies = [row["guild_update_ms"] for row in rows]
    return {"case": case, "repetition": repetition, "requests": len(rows), "elapsed_seconds": elapsed,
            "requests_per_second": len(rows) / elapsed, "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95), "p99_ms": percentile(latencies, 0.99),
            "guild_update_p95_ms": percentile(update_latencies, 0.95),
            "retries": sum(row["retries"] for row in rows), "lock_samples": len(observations),
            "samples_with_waits": sum(bool(sample["blocked"]) for sample in observations),
            "max_waiting_connections": max((len(sample["blocked"]) for sample in observations), default=0),
            "user_hash": digest(users), "guild_hash": digest(guilds), "verification": "passed"}


def experiment(requests_per_user):
    run_id = uuid4().hex
    folder = Path("results") / f"score-updates-{run_id}"
    folder.mkdir(parents=True)
    database = f"score_updates_{run_id}"
    admin_url = os.environ["ADMIN_DATABASE_URL"]
    admin = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": database})
    worker = make_conninfo(**{**conninfo_to_dict(os.environ["DATABASE_URL"]), "dbname": database})
    rng = random.Random(42)
    operations = {user: [rng.randint(1, 100) for _ in range(requests_per_user)]
                  for user in range(1, USERS + 1)}
    write_json(folder / "operations.json", operations)
    report = {"status": "failed", "folder": str(folder), "seed": 42, "users": USERS,
              "requests_per_user": requests_per_user, "repetitions": 5, "warmups_per_case": 1,
              "observer_interval_seconds": 0.02, "runs": []}
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        try:
            with psycopg.connect(admin, autocommit=True) as setup:
                setup.execute(Path("sql/score_updates.sql").read_text(encoding="utf-8"))
                report["environment"] = {
                    "postgresql": setup.execute("SELECT version()").fetchone()[0],
                    "db_settings": dict(setup.execute("SELECT name, setting FROM pg_settings "
                                                     "WHERE name IN ('shared_buffers', 'max_connections', "
                                                     "'synchronous_commit', 'fsync')").fetchall()),
                    "python": platform.python_version(), "psycopg": psycopg.__version__,
                    "runner_cpu_count": os.cpu_count(), "runner_platform": platform.platform()}
            with psycopg.connect(worker) as check:
                report["environment"]["worker_settings"] = dict(check.execute(
                    "SELECT name, setting FROM pg_settings WHERE name IN "
                    "('lock_timeout', 'statement_timeout', 'default_transaction_isolation')").fetchall())
            for repetition in range(6):
                order = ["concentrated", "distributed"]
                if repetition % 2:
                    order.reverse()
                for case in order:
                    result = run_case(admin, worker, folder, case, repetition, operations)
                    report["runs"].append(result)
                    print(json.dumps(result), flush=True)
            report["medians"] = {}
            for case in ("concentrated", "distributed"):
                measured = [run for run in report["runs"] if run["case"] == case and run["repetition"] > 0]
                if len({run["user_hash"] for run in measured}) != 1 or len({run["guild_hash"] for run in measured}) != 1:
                    raise RuntimeError("반복 실행의 점수 결과가 달라졌습니다.")
                report["medians"][case] = {key: statistics.median(run[key] for run in measured)
                    for key in ("elapsed_seconds", "requests_per_second", "p50_ms", "p95_ms", "p99_ms",
                                "guild_update_p95_ms", "max_waiting_connections")}
            # 길드 갱신이 실패하면 먼저 실행한 유저 점수 증가도 롤백되는지 확인.
            with psycopg.connect(admin, autocommit=True) as check:
                with check.transaction():
                    user_score, guild_id = check.execute("SELECT score, guild_id FROM user_score WHERE user_id=1").fetchone()
                    check.execute("DELETE FROM guild_score WHERE guild_id=%s", (guild_id,))
                    try:
                        apply_score(check, 1, guild_id, 1)
                    except ValueError:
                        pass
                    else:
                        raise RuntimeError("없는 길드의 점수 갱신이 실패하지 않았습니다.")
                    if check.execute("SELECT score FROM user_score WHERE user_id=1").fetchone()[0] != user_score:
                        raise RuntimeError("길드 갱신 실패 후 유저 점수가 변경됐습니다.")
                    raise psycopg.Rollback()
            report["rollback_check"] = "passed"
            report["status"] = "passed"
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            write_json(folder / "summary.json", report)
    print(json.dumps({"status": report["status"], "folder": str(folder), "medians": report["medians"]}, indent=2))
