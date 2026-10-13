BEGIN;

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
    UNIQUE (season_id, settlement_date),
    CHECK (state = 'DRAFT' OR input_cutoff IS NOT NULL),
    CHECK ((state = 'COMPLETED') = (completed_at IS NOT NULL)),
    CHECK (completed_at IS NULL OR completed_at >= input_cutoff)
);

CREATE TABLE score_event (
    event_id BIGINT PRIMARY KEY CHECK (event_id > 0),
    job_id BIGINT NOT NULL REFERENCES settlement_job (job_id),
    guild_id BIGINT NOT NULL REFERENCES guild (guild_id),
    score BIGINT NOT NULL CHECK (score >= 0)
);

CREATE TABLE settlement_result (
    job_id BIGINT NOT NULL REFERENCES settlement_job (job_id),
    guild_id BIGINT NOT NULL REFERENCES guild (guild_id),
    total_score BIGINT NOT NULL CHECK (total_score >= 0),
    reward_points BIGINT NOT NULL CHECK (reward_points >= 0),
    completed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (job_id, guild_id),
    CHECK (reward_points = total_score / 100)
);

CREATE TABLE guild_wallet (
    guild_id BIGINT PRIMARY KEY REFERENCES guild (guild_id),
    balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0)
);

CREATE TABLE settlement_chunk (
    job_id BIGINT NOT NULL REFERENCES settlement_job (job_id),
    chunk_no BIGINT NOT NULL CHECK (chunk_no > 0),
    first_guild_id BIGINT NOT NULL REFERENCES guild (guild_id),
    last_guild_id BIGINT NOT NULL REFERENCES guild (guild_id),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, chunk_no),
    CHECK (first_guild_id <= last_guild_id)
);

REVOKE ALL ON guild, settlement_job, score_event, settlement_result,
    guild_wallet, settlement_chunk FROM PUBLIC, lab_worker;
GRANT SELECT ON guild, settlement_job, score_event, settlement_result,
    guild_wallet, settlement_chunk TO lab_worker;
GRANT INSERT ON settlement_result TO lab_worker;
GRANT UPDATE (balance) ON guild_wallet TO lab_worker;
GRANT UPDATE (state, completed_at) ON settlement_job TO lab_worker;
GRANT UPDATE (completed_at) ON settlement_chunk TO lab_worker;

COMMIT;
