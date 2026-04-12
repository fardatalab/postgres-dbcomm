PROJ="/home/ubuntu/denischen/postgres-dbcomm"
gcc $PROJ/fdl_utils/printtup_dump/printtup_binary_dump_reserialize.c -o $PROJ/fdl_utils/printtup_dump/printtup_binary_dump_reserialize
clear 
$PROJ/fdl_utils/printtup_dump/printtup_binary_dump_reserialize $PROJ/dumps/printtup_binary_dump_sample.bin