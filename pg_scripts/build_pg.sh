cd /home/ubuntu/denischen/postgres-dbcomm

meson setup --wipe --prefix=/home/ubuntu/denischen/postgres-dbcomm/pginstall pgbuild
cd pgbuild
ninja
ninja install