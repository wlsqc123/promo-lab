import hashlib
import json
import os
import random
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg


TABLES = ("guild", "settlement_job", "score_event", "settlement_result",
          "guild_wallet", "settlement_chunk")
EXPECTED = Path(os.environ.get("RESULTS_DIR", "results/job-1")) / "expected.json"


def digest(rows):
    return hashlib.sha256(json.dumps(rows, separators=(",", ":"), default=str).encode()).hexdigest()


def save_report(command, report):
    folder = EXPECTED.parent
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{command}-{uuid4().hex}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report": str(path)}, ensure_ascii=False, indent=2))


def prepare(args):
    # 현재 단계는 빈 DB에 정산 1개만 준비. 여러 정산의 누적 비교는 후속 실험에서 추가.
    rng = random.Random(args.seed)
    totals = [0] * args.guilds
    events = []
    # 마지막 길드는 이벤트 없이 준비해 0점 결과도 확인.
    for event_id in range(1, args.events + 1):
        guild_id = rng.randint(1, args.guilds - 1)
        score = rng.randint(0, 1000)
        events.append((event_id, 1, guild_id, score))
        totals[guild_id - 1] += score
    chunks = [(1, number, first, min(first + args.chunk_size - 1, args.guilds))
              for number, first in enumerate(range(1, args.guilds + 1, args.chunk_size), 1)]
    expected = {
        "job_id": 1, "seed": args.seed, "guilds": args.guilds, "events": args.events,
        "chunk_size": args.chunk_size, "chunks": chunks, "input_hash": digest(events),
        "results": [(1, guild_id, total, total // 100)
                    for guild_id, total in enumerate(totals, 1)],
    }
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], autocommit=True) as conn:
        with conn.transaction():
            # 빈 테이블 확인과 초기 데이터 저장을 묶어 동시에 준비하는 요청 차단.
            conn.execute("LOCK TABLE " + ", ".join(TABLES) + " IN ACCESS EXCLUSIVE MODE")
            for table in TABLES:
                if conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0]:
                    raise ValueError("데이터 준비는 6개 테이블이 비어 있을 때만 가능합니다.")
            EXPECTED.parent.mkdir(parents=True, exist_ok=True)
            # SQL 집계 없이 계산한 기대값을 DB 입력 전에 저장.
            with EXPECTED.open("x", encoding="utf-8") as file:
                json.dump(expected, file, ensure_ascii=False, indent=2)
            conn.execute("INSERT INTO settlement_job (job_id, season_id, settlement_date) "
                         "VALUES (1, 1, %s)", (datetime.now(timezone.utc).date(),))
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO guild VALUES (%s, %s)",
                                [(i, f"길드 {i}") for i in range(1, args.guilds + 1)])
                cur.executemany("INSERT INTO guild_wallet (guild_id) VALUES (%s)",
                                [(i,) for i in range(1, args.guilds + 1)])
                cur.executemany("INSERT INTO settlement_chunk "
                                "(job_id, chunk_no, first_guild_id, last_guild_id) "
                                "VALUES (%s, %s, %s, %s)", chunks)
                with cur.copy("COPY score_event (event_id, job_id, guild_id, score) FROM STDIN") as copy:
                    for event in events:
                        copy.write_row(event)
            conn.execute("UPDATE settlement_job SET state='READY', "
                         "input_cutoff=clock_timestamp() WHERE job_id=1")
        conn.execute("ANALYZE score_event")
    save_report("prepare", {"status": "READY", "guilds": args.guilds,
                            "events": args.events, "chunks": len(chunks),
                            "expected": str(EXPECTED)})


def settle_chunk(conn, chunk_no):
    with conn.transaction():
        chunk = conn.execute("SELECT first_guild_id, last_guild_id, completed_at "
                             "FROM settlement_chunk WHERE job_id=1 AND chunk_no=%s "
                             "FOR UPDATE", (chunk_no,)).fetchone()
        if chunk is None:
            raise ValueError("처리할 청크가 없습니다.")
        first, last, completed = chunk
        if completed is not None:
            return False
        scores = dict(conn.execute("SELECT guild_id, SUM(score) FROM score_event "
                                   "WHERE job_id=1 AND guild_id BETWEEN %s AND %s "
                                   "GROUP BY guild_id", (first, last)).fetchall())
        guilds = conn.execute("SELECT guild_id FROM guild WHERE guild_id BETWEEN %s AND %s "
                              "ORDER BY guild_id", (first, last)).fetchall()
        if len(guilds) != last - first + 1:
            raise ValueError("청크 안의 대상 길드가 누락됐습니다.")
        for (guild_id,) in guilds:
            total = scores.get(guild_id, 0)
            reward = total // 100
            conn.execute("INSERT INTO settlement_result VALUES (1, %s, %s, %s, clock_timestamp())",
                         (guild_id, total, reward))
            updated = conn.execute("UPDATE guild_wallet SET balance=balance+%s WHERE guild_id=%s",
                                   (reward, guild_id))
            if updated.rowcount != 1:
                raise ValueError("보상을 넣을 지갑이 없습니다.")
        conn.execute("UPDATE settlement_chunk SET completed_at=clock_timestamp() "
                     "WHERE job_id=1 AND chunk_no=%s", (chunk_no,))
        pause_for_termination(conn, chunk_no, "before-commit")
    pause_for_termination(conn, chunk_no, "after-commit")
    return True


def pause_for_termination(conn, chunk_no, point):
    # 종료 실험에서 첫 청크만 지정 지점에 멈추고, 부모 프로세스가 SIGKILL로 종료.
    if chunk_no == 1 and os.environ.get("CRASH_POINT") == point:
        marker = Path(os.environ["CRASH_MARKER"])
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps({"point": point, "chunk_no": chunk_no,
                                        "backend_pid": conn.info.backend_pid}), encoding="utf-8")
        temporary.replace(marker)
        signal.pause()


def settle():
    start = time.perf_counter()
    processed = skipped = retries = 0
    retry_events = []
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True,
                         application_name=os.environ.get("WORKER_NAME", "settlement-worker")) as conn:
        job = conn.execute("SELECT state FROM settlement_job WHERE job_id=1").fetchone()
        if job is None or job[0] not in ("READY", "COMPLETED"):
            raise ValueError("먼저 데이터를 준비해야 합니다.")
        chunks = conn.execute("SELECT chunk_no FROM settlement_chunk WHERE job_id=1 "
                              "ORDER BY chunk_no").fetchall()
        if not chunks:
            raise ValueError("청크 목록이 비어 있습니다.")
        for (chunk_no,) in chunks:
            for attempt in range(4):
                try:
                    if settle_chunk(conn, chunk_no):
                        processed += 1
                    else:
                        skipped += 1
                    break
                except psycopg.Error as exc:
                    if exc.sqlstate not in ("40P01", "55P03", "40001") or attempt == 3:
                        raise
                    retries += 1
                    event = {"chunk": chunk_no, "retry": attempt + 1, "sqlstate": exc.sqlstate}
                    retry_events.append(event)
                    print(json.dumps(event), flush=True)
                    time.sleep(0.05 * 2 ** attempt + random.uniform(0, 0.05))
        with conn.transaction():
            conn.execute("UPDATE settlement_job SET state='COMPLETED', "
                         "completed_at=clock_timestamp() WHERE job_id=1 AND state='READY' "
                         "AND NOT EXISTS (SELECT 1 FROM settlement_chunk "
                         "WHERE job_id=1 AND completed_at IS NULL)")
    save_report("settle", {"processed_chunks": processed, "skipped_chunks": skipped,
                           "retries": retries, "retry_events": retry_events,
                           "elapsed_seconds": time.perf_counter() - start})


def compare(conn, expected):
    results = conn.execute("SELECT job_id, guild_id, total_score, reward_points "
                           "FROM settlement_result ORDER BY job_id, guild_id").fetchall()
    wallets = conn.execute("SELECT guild_id, balance FROM guild_wallet ORDER BY guild_id").fetchall()
    chunks = conn.execute("SELECT job_id, chunk_no, first_guild_id, last_guild_id, completed_at "
                          "FROM settlement_chunk ORDER BY job_id, chunk_no").fetchall()
    wanted_results = [tuple(row) for row in expected["results"]]
    wanted_wallets = [(row[1], row[3]) for row in wanted_results]
    if results != wanted_results:
        raise ValueError("길드별 점수·보상 또는 결과 목록이 예상값과 다릅니다.")
    if wallets != wanted_wallets:
        raise ValueError("길드별 잔액 또는 지갑 목록이 예상값과 다릅니다.")
    if [row[:4] for row in chunks] != [tuple(row) for row in expected["chunks"]]:
        raise ValueError("청크 범위 또는 목록이 준비한 값과 다릅니다.")
    if any(row[4] is None for row in chunks):
        raise ValueError("완료되지 않은 청크가 있습니다.")
    jobs = conn.execute("SELECT job_id, state, completed_at FROM settlement_job ORDER BY job_id").fetchall()
    if len(jobs) != 1 or jobs[0][0:2] != (1, "COMPLETED") or jobs[0][2] is None:
        raise ValueError("정산 완료 상태가 아닙니다.")
    guilds = conn.execute("SELECT guild_id FROM guild ORDER BY guild_id").fetchall()
    if guilds != [(i,) for i in range(1, expected["guilds"] + 1)]:
        raise ValueError("준비한 길드 목록과 다릅니다.")
    events = conn.execute("SELECT event_id, job_id, guild_id, score FROM score_event ORDER BY event_id").fetchall()
    if len(events) != expected["events"] or digest(events) != expected["input_hash"]:
        raise ValueError("확정된 입력이 변경됐습니다.")
    # 완료 시각도 포함해 재실행 전후 결과·잔액 변경 확인.
    full_results = conn.execute("SELECT * FROM settlement_result ORDER BY job_id, guild_id").fetchall()
    return {"status": "passed", "guilds": len(results), "events": len(events),
            "chunks": len(chunks), "result_hash": digest(results),
            "wallet_hash": digest(wallets), "data_hash": digest([full_results, wallets, chunks, jobs])}


def verify():
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            report = compare(conn, expected)
    save_report("verify", report)
