NUM_ROWS=5000000
dir="/home/ubuntu/denischen"

rm -f $dir/postgres-dbcomm/pgdata/printtup_binary_dump_sample.bin
$dir/postgres-dbcomm/fdl_utils/printtup_dump/printtup_binary_dump_client --conninfo "dbname=postgres host=/tmp" --query "SELECT * FROM public.sample LIMIT $NUM_ROWS;"
cp -f $dir/postgres-dbcomm/pgdata/printtup_binary_dump_sample.bin $dir/postgres-dbcomm/dumps/printtup_binary_dump_sample_$NUM_ROWS.bin
cp -f $dir/postgres-dbcomm/pgdata/printtup_binary_dump_sample.bin $dir/Homer-DPU-DB-Comm/dumps/printtup_binary_dump_sample_$NUM_ROWS.bin