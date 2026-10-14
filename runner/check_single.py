"""완료된 기본 실험 데이터에서 실행. 모든 변경은 롤백."""
import json
import os

import psycopg

from runner.settlement import EXPECTED, compare, settle_chunk


expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], autocommit=True) as conn:
    baseline = compare(conn, expected)
    cases = [
        "UPDATE guild_wallet SET balance=balance+1 WHERE guild_id=1",
        "DELETE FROM settlement_result WHERE job_id=1 AND guild_id=1",
        "UPDATE score_event SET score=score+1 WHERE event_id=1",
        "INSERT INTO score_event VALUES (9223372036854775807, 999, 999, 0)",
        "UPDATE settlement_chunk SET completed_at=NULL WHERE job_id=1 AND chunk_no=1",
    ]
    for statement in cases:
        with conn.transaction():
            conn.execute(statement)
            try:
                compare(conn, expected)
            except ValueError:
                pass
            else:
                raise AssertionError(f"결과 비교에서 오류를 놓쳤습니다: {statement}")
            raise psycopg.Rollback()

    with conn.transaction():
        conn.execute("UPDATE settlement_chunk SET completed_at=NULL WHERE job_id=1 AND chunk_no=1")
        try:
            settle_chunk(conn, 1)
        except psycopg.errors.UniqueViolation:
            pass
        else:
            raise AssertionError("중복 결과가 실패하지 않았습니다.")
        assert conn.execute("SELECT balance FROM guild_wallet WHERE guild_id=1").fetchone()[0] == expected["results"][0][3]
        raise psycopg.Rollback()

    with conn.transaction():
        first, last = expected["chunks"][0][2:4]
        conn.execute("UPDATE settlement_chunk SET completed_at=NULL WHERE job_id=1 AND chunk_no=1")
        conn.execute("DELETE FROM settlement_result WHERE job_id=1 AND guild_id BETWEEN %s AND %s",
                     (first, last))
        conn.execute("DELETE FROM guild_wallet WHERE guild_id=%s", (last,))
        before_wallets = conn.execute("SELECT guild_id, balance FROM guild_wallet ORDER BY guild_id").fetchall()
        try:
            settle_chunk(conn, 1)
        except ValueError as exc:
            assert "지갑" in str(exc)
        else:
            raise AssertionError("지갑 누락이 실패하지 않았습니다.")
        assert conn.execute("SELECT count(*) FROM settlement_result WHERE job_id=1 "
                            "AND guild_id BETWEEN %s AND %s", (first, last)).fetchone()[0] == 0
        assert conn.execute("SELECT completed_at FROM settlement_chunk "
                            "WHERE job_id=1 AND chunk_no=1").fetchone()[0] is None
        assert conn.execute("SELECT guild_id, balance FROM guild_wallet ORDER BY guild_id").fetchall() == before_wallets
        raise psycopg.Rollback()
    assert compare(conn, expected) == baseline
print("결과 비교 오류 5종, 중복 결과 거부, 청크 롤백 확인 통과. 변경 데이터는 모두 롤백했습니다.")
