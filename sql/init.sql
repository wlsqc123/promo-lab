CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE lab_worker LOGIN PASSWORD 'local_lab_worker';
GRANT CONNECT ON DATABASE settlement_lab TO lab_worker;
GRANT USAGE ON SCHEMA public TO lab_worker;
ALTER ROLE lab_worker SET timezone = 'UTC';
ALTER ROLE lab_worker SET default_transaction_isolation = 'read committed';
ALTER ROLE lab_worker SET lock_timeout = '2s';
ALTER ROLE lab_worker SET statement_timeout = '30s';
-- 테이블별 권한은 DDL 구현 시 부여한다. 입력 수정 권한은 부여하지 않는다.
