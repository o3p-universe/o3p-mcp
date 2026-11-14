-- Set up the SQL sandbox of o3p_mcp on a dedicated PostgreSQL instance.
--
-- Run as a superuser of the SANDBOX instance (never the instance holding the
-- Odoo database):
--
--   psql -h <sandbox host> -U postgres -d postgres \
--        -v password='<O3P_SQL_PASSWORD>' -v roles=4 \
--        -v prefix=o3p_sqlbox -v database=o3p_sqlbox \
--        -f sandbox-setup.sql
--
-- Creates an empty database and the login roles <prefix>_1 … <prefix>_<roles>
-- (one per concurrent query, CONNECTION LIMIT 1, all with the same password),
-- with no attribute, no membership and no privilege except CONNECT and
-- TEMPORARY on that database. Every other database of the instance is closed
-- to PUBLIC. Safe to run again: existing roles get the password and limits
-- again.
\set ON_ERROR_STOP on

SELECT format('CREATE DATABASE %I', :'database')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'database') \gexec

SELECT format('CREATE ROLE %I', :'prefix' || '_' || n)
  FROM generate_series(1, :roles) AS n
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'prefix' || '_' || n) \gexec

SELECT format(
         'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE '
         'NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1', role, :'password'),
       format('ALTER ROLE %I SET temp_file_limit = %L', role, '256MB'),
       format('ALTER ROLE %I SET work_mem = %L', role, '32MB'),
       format('ALTER ROLE %I SET statement_timeout = %L', role, '300s'),
       format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'database', role)
  FROM (SELECT :'prefix' || '_' || n AS role FROM generate_series(1, :roles) AS n) AS roles \gexec

-- Only the sandbox roles may connect to the sandbox; nobody else but the
-- superuser may connect anywhere.
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', datname)
  FROM pg_database WHERE datallowconn \gexec

\connect :"database"
REVOKE ALL ON SCHEMA public FROM PUBLIC;
