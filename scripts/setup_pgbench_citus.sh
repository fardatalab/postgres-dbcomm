#!/usr/bin/env bash

set -euo pipefail

# Initialize the standard pgbench schema and then convert the built-in tables
# into distributed Citus tables. The scale defaults to 32 so the built-in TPC-B
# style tables are large enough for the planned 1..32 concurrency sweep; pgbench
# recommends the scale factor be at least as large as the largest client count.

scale="${1:-32}"
dbname="${2:-postgres}"

pgbench_bin="${PGBENCH_BIN:-${HOME}/pg/bin/pgbench}"
psql_bin="${PSQL_BIN:-${HOME}/pg/bin/psql}"
pguser="${PGUSER:-JasonHu}"
pghost="${PGHOST:-127.0.0.1}"
pgport="${PGPORT:-5432}"

#
# Rebuild the benchmark tables from scratch so repeated setup runs do not keep
# the old shard files around in the data directory. This keeps subsequent
# physical base backups much smaller and makes the later standby bootstrap less
# expensive.
"${psql_bin}" -U "${pguser}" -h "${pghost}" -p "${pgport}" -d "${dbname}" -v ON_ERROR_STOP=1 <<'SQL'
DROP TABLE IF EXISTS pgbench_history CASCADE;
DROP TABLE IF EXISTS pgbench_tellers CASCADE;
DROP TABLE IF EXISTS pgbench_branches CASCADE;
DROP TABLE IF EXISTS pgbench_accounts CASCADE;
SQL

"${pgbench_bin}" -i -s "${scale}" -U "${pguser}" -h "${pghost}" -p "${pgport}" "${dbname}"

"${psql_bin}" -U "${pguser}" -h "${pghost}" -p "${pgport}" -d "${dbname}" -v ON_ERROR_STOP=1 <<'SQL'
SELECT create_distributed_table('pgbench_accounts', 'aid');
SELECT create_distributed_table('pgbench_branches', 'bid');
SELECT create_distributed_table('pgbench_tellers', 'tid');
SELECT create_distributed_table('pgbench_history', 'aid', colocate_with => 'pgbench_accounts');
SELECT truncate_local_data_after_distributing_table('pgbench_accounts');
SELECT truncate_local_data_after_distributing_table('pgbench_branches');
SELECT truncate_local_data_after_distributing_table('pgbench_tellers');
SELECT truncate_local_data_after_distributing_table('pgbench_history');
SQL
