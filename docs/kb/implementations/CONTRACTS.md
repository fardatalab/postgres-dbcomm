# Implementations — Contracts And Invariants

<!-- kb-summary: Cross-repository build and ABI contracts shared by the PostgreSQL-facing and Citus-side Homer implementations. -->

> Start contract lookup in the narrowest owning implementation directory, then walk upward to this root. This index contains only rules that genuinely cross the Citus/PostgreSQL implementation split. Verify every pointer in current source.

## Symbol entries

### Installed `libhomer_client.a` — paired Citus/PostgreSQL build artifact

- **MEANS:** The Homer client implementation and ABI headers are built by `citus-dbcomm`; the installed static archive is then linked into PostgreSQL-side consumers, including the `postgres` executable and `pgbench`.
- **DOES NOT MEAN:** Installing a new `citus.so`, copying headers, or changing only the Citus source tree updates already-linked PostgreSQL consumers.
- **CONTRACT / INVARIANT:** A Homer client implementation or ABI change requires building and installing the Citus archive before relinking every affected PostgreSQL consumer. The installed archive, installed headers, and linked consumers form one compatibility unit.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** archive definition and installation in `/data/dbcomm/citus-dbcomm/Makefile:16`, `:118`, and `:312`; backend link in `src/backend/meson.build:47`; pgbench link in `src/bin/pgbench/meson.build:43`.
- **TEMPTING WRONG MOVE:** Installing `citus.so` and starting PostgreSQL before relinking `postgres` can leave runtime-resolved Homer symbols or ABI expectations out of sync.

## Related

- [citus/CONTRACTS.md](citus/CONTRACTS.md)
- [postgres/CONTRACTS.md](postgres/CONTRACTS.md)
- [../operations/farnet_operational_hazards.md](../operations/farnet_operational_hazards.md)
