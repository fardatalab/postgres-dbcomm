export PG_PRINTTUP_BINARY_DUMP=1
export PG_PRINTTUP_BINARY_DUMP_FILE=printtup_binary_dump_sample.bin
/home/ubuntu/denischen/postgres-dbcomm/pginstall/bin/pg_ctl -D /home/ubuntu/denischen/postgres-dbcomm/pgdata -l /home/ubuntu/denischen/postgres-dbcomm/pgdata/logfile start
