-- 유저 점수와 미반영 증가분을 함께 커밋해 작업자 재시작 시에도 내역 유지.
CREATE TABLE pending_score (
    request_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL CHECK (guild_id > 0),
    delta BIGINT NOT NULL CHECK (delta > 0)
);
GRANT SELECT, INSERT, DELETE ON pending_score TO lab_worker;
-- FOR UPDATE에 필요한 권한만 한 컬럼에 부여. 실제 ID 갱신은 수행하지 않음.
GRANT UPDATE (request_id) ON pending_score TO lab_worker;
