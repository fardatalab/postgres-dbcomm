cd /home/ubuntu/denischen/dchen-postgres-dbcomm

meson setup --wipe --prefix=/home/ubuntu/denischen/dchen-postgres-dbcomm/pginstall pgbuild
cd pgbuild
ninja
ninja install