#!/bin/sh
# Docker entry point hook for the postgres image: runs sandbox-setup.sql once,
# when the sandbox instance is first initialised. Configure it with
#   O3P_SQL_PASSWORD (required), O3P_SQL_ROLES (default 4),
#   O3P_SQL_USER (role prefix, default o3p_sqlbox),
#   O3P_SQL_DATABASE (default o3p_sqlbox).
# Mount it into /docker-entrypoint-initdb.d/ and sandbox-setup.sql at
# /o3p/sandbox-setup.sql.
set -eu
: "${O3P_SQL_PASSWORD:?O3P_SQL_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v password="$O3P_SQL_PASSWORD" \
    -v roles="${O3P_SQL_ROLES:-4}" \
    -v prefix="${O3P_SQL_USER:-o3p_sqlbox}" \
    -v database="${O3P_SQL_DATABASE:-o3p_sqlbox}" \
    -f /o3p/sandbox-setup.sql
