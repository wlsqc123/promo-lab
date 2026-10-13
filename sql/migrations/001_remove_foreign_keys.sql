BEGIN;
ALTER TABLE score_event
    DROP CONSTRAINT score_event_job_id_fkey,
    DROP CONSTRAINT score_event_guild_id_fkey,
    ADD CHECK (job_id > 0),
    ADD CHECK (guild_id > 0);
ALTER TABLE settlement_result
    DROP CONSTRAINT settlement_result_job_id_fkey,
    DROP CONSTRAINT settlement_result_guild_id_fkey,
    ADD CHECK (job_id > 0),
    ADD CHECK (guild_id > 0);
ALTER TABLE guild_wallet
    DROP CONSTRAINT guild_wallet_guild_id_fkey,
    ADD CHECK (guild_id > 0);
ALTER TABLE settlement_chunk
    DROP CONSTRAINT settlement_chunk_job_id_fkey,
    DROP CONSTRAINT settlement_chunk_first_guild_id_fkey,
    DROP CONSTRAINT settlement_chunk_last_guild_id_fkey,
    ADD CHECK (job_id > 0),
    ADD CHECK (first_guild_id > 0),
    ADD CHECK (last_guild_id > 0);
COMMIT;
