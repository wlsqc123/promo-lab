CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE lab_worker LOGIN PASSWORD 'local_lab_worker';
GRANT CONNECT ON DATABASE settlement_lab TO lab_worker;
GRANT USAGE ON SCHEMA public TO lab_worker;
ALTER ROLE lab_worker SET timezone = 'UTC';
ALTER ROLE lab_worker SET default_transaction_isolation = 'read committed';
ALTER ROLE lab_worker SET lock_timeout = '2s';
ALTER ROLE lab_worker SET statement_timeout = '30s';
-- 업무 테이블과 권한은 다음 초기화 파일 schema.sql에서 구성한다.
