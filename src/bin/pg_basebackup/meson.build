# Copyright (c) 2022-2024, PostgreSQL Global Development Group

common_sources = files(
  'bbstreamer_file.c',
  'bbstreamer_gzip.c',
  'bbstreamer_inject.c',
  'bbstreamer_lz4.c',
  'bbstreamer_tar.c',
  'bbstreamer_zstd.c',
  'receivelog.c',
  'streamutil.c',
  'walmethods.c',
)

pg_basebackup_deps = [frontend_code, libpq, lz4, zlib, zstd]
pg_basebackup_common = static_library('libpg_basebackup_common',
  common_sources,
  dependencies: pg_basebackup_deps,
  kwargs: internal_lib_args,
)

pg_basebackup_sources = files(
  'pg_basebackup.c',
)

if host_system == 'windows'
  pg_basebackup_sources += rc_bin_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'pg_basebackup',
    '--FILEDESC', 'pg_basebackup - streaming WAL and backup receivers',])
endif

# Homer receiver launcher: pg_basebackup --homer-receive drives the selected-DPU
# basebackup RECEIVE-consume client, whose implementation lives in the Citus tree's
# prebuilt libhomer_client.a (built with -DHOMER_CLIENT_DPU_WITH_DOCA).  Mirror the
# backend's link recipe (src/backend/meson.build): pull DOCA in as pkg-config deps and
# add the installed server include dir just for the Homer ABI headers.  The include dir
# is marked is_system so it is -isystem (lower priority than the frontend headers, no
# shadowing) -- the header is frontend-safe (stdint/stdbool + Homer ABI; DOCA handles are
# void*), so this pulls in no server headers.  The static archive is added as a raw link
# arg + link dependency, exactly like the backend.
homer_client_static = '/data/dbcomm/pg-citus/lib/x86_64-linux-gnu/libhomer_client.a'
homer_receive_dep = declare_dependency(
  include_directories: include_directories('/data/dbcomm/pg-citus/include/postgresql/server', is_system: true),
  dependencies: [dependency('doca-dma', required: true), dependency('doca-common', required: true)],
)

pg_basebackup = executable('pg_basebackup',
  pg_basebackup_sources,
  link_with: [pg_basebackup_common],
  dependencies: pg_basebackup_deps + [homer_receive_dep],
  link_args: [homer_client_static],
  link_depends: [homer_client_static],
  kwargs: default_bin_args,
)
bin_targets += pg_basebackup


pg_createsubscriber_sources = files(
  'pg_createsubscriber.c'
)

if host_system == 'windows'
  pg_createsubscriber_sources += rc_bin_gen.process(win32ver_rc, extra_args: [
	'--NAME', 'pg_createsubscriber',
	'--FILEDESC', 'pg_createsubscriber - create a new logical replica from a standby server',])
endif

pg_createsubscriber = executable('pg_createsubscriber',
  pg_createsubscriber_sources,
  dependencies: [frontend_code, libpq],
  kwargs: default_bin_args,
)
bin_targets += pg_createsubscriber


pg_receivewal_sources = files(
  'pg_receivewal.c',
)

if host_system == 'windows'
  pg_receivewal_sources += rc_bin_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'pg_receivewal',
    '--FILEDESC', 'pg_receivewal - streaming WAL and backup receivers',])
endif

pg_receivewal = executable('pg_receivewal',
  pg_receivewal_sources,
  link_with: [pg_basebackup_common],
  dependencies: pg_basebackup_deps,
  kwargs: default_bin_args,
)
bin_targets += pg_receivewal


pg_recvlogical_sources = files(
  'pg_recvlogical.c',
)

if host_system == 'windows'
  pg_recvlogical_sources += rc_bin_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'pg_recvlogical',
    '--FILEDESC', 'pg_recvlogical - streaming WAL and backup receivers',])
endif

pg_recvlogical = executable('pg_recvlogical',
  pg_recvlogical_sources,
  link_with: [pg_basebackup_common],
  dependencies: pg_basebackup_deps,
  kwargs: default_bin_args,
)
bin_targets += pg_recvlogical

tests += {
  'name': 'pg_basebackup',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'tap': {
    'env': {'GZIP_PROGRAM': gzip.found() ? gzip.path() : '',
            'TAR': tar.found() ? tar.path() : '',
            'LZ4': program_lz4.found() ? program_lz4.path() : '',
    },
    'tests': [
      't/010_pg_basebackup.pl',
      't/011_in_place_tablespace.pl',
      't/020_pg_receivewal.pl',
      't/030_pg_recvlogical.pl',
      't/040_pg_createsubscriber.pl',
    ],
  },
}

subdir('po', if_found: libintl)
