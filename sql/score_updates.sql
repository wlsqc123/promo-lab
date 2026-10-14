-- 점수 갱신 실험 전용. 정산 테이블과 분리하고 FK는 사용하지 않음.
CREATE TABLE guild_score (
    guild_id BIGINT PRIMARY KEY,
    score BIGINT NOT NULL DEFAULT 0 CHECK (score >= 0)
);
CREATE TABLE user_score (
    user_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    score BIGINT NOT NULL DEFAULT 0 CHECK (score >= 0)
);
GRANT SELECT ON guild_score, user_score TO lab_worker;
GRANT UPDATE (score) ON guild_score, user_score TO lab_worker;
