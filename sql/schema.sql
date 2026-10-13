BEGIN;

-- FK 미사용. 데이터 준비 시 ID 확인, 실험 중 정산·길드와 확정된 입력 변경 금지.
CREATE TABLE guild (
    guild_id BIGINT PRIMARY KEY CHECK (guild_id > 0),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100)
);

CREATE TABLE settlement_job (
    job_id BIGINT PRIMARY KEY CHECK (job_id > 0),
    season_id BIGINT NOT NULL CHECK (season_id > 0),
    settlement_date DATE NOT NULL,
    state TEXT NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT', 'READY', 'COMPLETED')),
    input_cutoff TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (season_id, settlement_date), -- 같은 시즌·날짜의 정산 중복 방지
    CHECK (state = 'DRAFT' OR input_cutoff IS NOT NULL),
    CHECK ((state = 'COMPLETED') = (completed_at IS NOT NULL)),
    CHECK (completed_at IS NULL OR completed_at >= input_cutoff)
);

-- 인덱스 비교의 기준: PK 인덱스만 사용.
CREATE TABLE score_event (
    event_id BIGINT PRIMARY KEY CHECK (event_id > 0),
    job_id BIGINT NOT NULL CHECK (job_id > 0),
    guild_id BIGINT NOT NULL CHECK (guild_id > 0),
    score BIGINT NOT NULL CHECK (score >= 0)
);

CREATE TABLE settlement_result (
    job_id BIGINT NOT NULL CHECK (job_id > 0),
    guild_id BIGINT NOT NULL CHECK (guild_id > 0),
    total_score BIGINT NOT NULL CHECK (total_score >= 0),
    reward_points BIGINT NOT NULL CHECK (reward_points >= 0),
    completed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (job_id, guild_id), -- 같은 정산·길드의 결과 중복 방지
    CHECK (reward_points = total_score / 100) -- 원본 점수 합계는 프로그램에서 별도 확인
);

CREATE TABLE guild_wallet (
    guild_id BIGINT PRIMARY KEY CHECK (guild_id > 0),
    balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0)
);

-- 시작·끝 ID는 처리 범위. 해당 ID의 길드가 실제로 존재할 필요는 없음.
-- 완료 표시 전 결과 저장·잔액 증가를 같은 트랜잭션으로 처리.
CREATE TABLE settlement_chunk (
    job_id BIGINT NOT NULL CHECK (job_id > 0),
    chunk_no BIGINT NOT NULL CHECK (chunk_no > 0),
    first_guild_id BIGINT NOT NULL CHECK (first_guild_id > 0),
    last_guild_id BIGINT NOT NULL CHECK (last_guild_id > 0),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, chunk_no),
    CHECK (first_guild_id <= last_guild_id)
);

-- 작업자: 조회·결과 추가·잔액 및 완료 상태 수정만 허용.
-- 입력 수정·결과 덮어쓰기/삭제·처리 범위 변경·DDL 권한은 부여하지 않음.
REVOKE ALL ON guild, settlement_job, score_event, settlement_result,
    guild_wallet, settlement_chunk FROM PUBLIC, lab_worker;
GRANT SELECT ON guild, settlement_job, score_event, settlement_result,
    guild_wallet, settlement_chunk TO lab_worker;
GRANT INSERT ON settlement_result TO lab_worker;
GRANT UPDATE (balance) ON guild_wallet TO lab_worker;
GRANT UPDATE (state, completed_at) ON settlement_job TO lab_worker;
GRANT UPDATE (completed_at) ON settlement_chunk TO lab_worker;

COMMIT;
