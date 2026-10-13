\set ON_ERROR_STOP on
BEGIN;
INSERT INTO guild VALUES (1, '검증 길드'), (2, '빈 길드');
INSERT INTO settlement_job (job_id, season_id, settlement_date) VALUES (1, 1, '2026-10-13');
INSERT INTO guild_wallet (guild_id) VALUES (1), (2);
INSERT INTO score_event VALUES (1, 1, 1, 199);
INSERT INTO settlement_chunk (job_id, chunk_no, first_guild_id, last_guild_id)
    VALUES (1, 1, 1, 2);
UPDATE settlement_job SET state = 'READY', input_cutoff = CURRENT_TIMESTAMP WHERE job_id = 1;

DO $$
BEGIN
    BEGIN
        INSERT INTO guild VALUES (1, '중복');
        RAISE EXCEPTION 'PK accepted duplicate';
    EXCEPTION WHEN unique_violation THEN NULL; END;
    BEGIN
        INSERT INTO settlement_job (job_id, season_id, settlement_date) VALUES (2, 1, '2026-10-13');
        RAISE EXCEPTION 'job unique accepted duplicate';
    EXCEPTION WHEN unique_violation THEN NULL; END;
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public' AND c.contype = 'f'
          AND t.relname IN ('guild', 'settlement_job', 'score_event',
                           'settlement_result', 'guild_wallet', 'settlement_chunk')
    ) THEN
        RAISE EXCEPTION 'lab tables must not have foreign keys';
    END IF;
    BEGIN
        INSERT INTO score_event VALUES (2, 0, 1, 0);
        RAISE EXCEPTION 'CHECK accepted nonpositive reference ID';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        INSERT INTO settlement_chunk VALUES (1, 2, 0, 1, NULL);
        RAISE EXCEPTION 'CHECK accepted nonpositive range boundary';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        INSERT INTO score_event VALUES (2, 1, 1, -1);
        RAISE EXCEPTION 'CHECK accepted negative score';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        INSERT INTO settlement_result VALUES (1, 1, 199, 2, CURRENT_TIMESTAMP);
        RAISE EXCEPTION 'CHECK accepted wrong reward';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        UPDATE guild_wallet SET balance = -1 WHERE guild_id = 1;
        RAISE EXCEPTION 'CHECK accepted negative balance';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        UPDATE settlement_job SET state = 'COMPLETED' WHERE job_id = 1;
        RAISE EXCEPTION 'CHECK accepted missing completion';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        INSERT INTO settlement_chunk VALUES (1, 2, 2, 1, NULL);
        RAISE EXCEPTION 'CHECK accepted reversed range';
    EXCEPTION WHEN check_violation THEN NULL; END;
    IF (SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'score_event') <> 1 THEN
        RAISE EXCEPTION 'baseline score_event must have only PK index';
    END IF;
END $$;

-- job·guild 존재 여부는 후속 생성기·검증기에서 확인. DB는 존재하지 않는 양수 ID도 허용.
INSERT INTO score_event VALUES (2, 999, 999, 0);
DELETE FROM score_event WHERE event_id = 2;
INSERT INTO settlement_chunk VALUES (1, 2, 100, 200, NULL);
DELETE FROM settlement_chunk WHERE job_id = 1 AND chunk_no = 2;

SET LOCAL ROLE lab_worker;
SELECT * FROM score_event;
SELECT * FROM settlement_chunk WHERE job_id = 1 FOR UPDATE;
INSERT INTO settlement_result VALUES (1, 1, 199, 1, CURRENT_TIMESTAMP), (1, 2, 0, 0, CURRENT_TIMESTAMP);
UPDATE guild_wallet SET balance = balance + 1 WHERE guild_id = 1;
UPDATE settlement_chunk SET completed_at = CURRENT_TIMESTAMP WHERE job_id = 1;
UPDATE settlement_job SET state = 'COMPLETED', completed_at = CURRENT_TIMESTAMP WHERE job_id = 1;
DO $$
BEGIN
    BEGIN
        INSERT INTO settlement_result VALUES (1, 1, 199, 1, CURRENT_TIMESTAMP);
        RAISE EXCEPTION 'result PK accepted duplicate';
    EXCEPTION WHEN unique_violation THEN NULL; END;
    BEGIN
        UPDATE score_event SET score = 0;
        RAISE EXCEPTION 'worker modified input';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        INSERT INTO score_event VALUES (2, 1, 1, 0);
        RAISE EXCEPTION 'worker inserted input';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        DELETE FROM settlement_result;
        RAISE EXCEPTION 'worker deleted results';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        UPDATE settlement_result SET total_score = 0;
        RAISE EXCEPTION 'worker overwrote results';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        UPDATE settlement_chunk SET first_guild_id = 2;
        RAISE EXCEPTION 'worker modified chunk range';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        UPDATE settlement_job SET input_cutoff = CURRENT_TIMESTAMP;
        RAISE EXCEPTION 'worker modified cutoff';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        CREATE TABLE public.forbidden (id bigint);
        RAISE EXCEPTION 'worker created table';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
ROLLBACK;
\echo 'Schema and worker permission checks passed; fixtures rolled back.'
