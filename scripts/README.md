# Benchmark Runbook

This repository is a PostgreSQL 17 tree plus a copied Citus tree that we use for benchmark experiments on the `node-0` to `node-4` cluster.

The canonical results directories to keep are:

- `bench-results-network-interference`
- `bench-results-tuned`
- `bench-results-vanilla`

The older `bench-results`, `bench-results-sample05`, `bench-results-sample20`, and `bench-results-test` trees are exploratory or intermediate runs.

## Cluster Assumptions

The scripts in this tree assume the following layout and install paths:

- `node-0`: coordinator for Citus, or vanilla primary when the vanilla setup scripts repurpose it
- `node-1`: Citus worker primary, and the generator host for the vanilla interference experiment
- `node-2`: Citus worker primary
- `node-3`: standby for `node-1`, or vanilla standby when the vanilla setup scripts repurpose it
- `node-4`: standby for `node-2`
- fast fabric: `enp23s0f0` on `10.10.1.0/24`
- install prefix on every node: `~/pg`
- SSH user: `JasonHu`

The current branch layout is also important:

- PostgreSQL: `pg-17-timings`
- Citus: copied `timings-jason`

## Setup Not Handled By The Scripts

Before running any benchmark, make sure these prerequisites are already true:

1. PostgreSQL and Citus are built and installed under `~/pg` on every node.
2. If a build tree was copied in with `scp`, it was reconfigured for this cluster before installation so generated paths point at `~/pg`.
3. The `10.10.1.0/24` fabric is allowed in `pg_hba.conf` for normal connections and replication connections.
4. The cluster nodes can SSH to each other with `BatchMode=yes`.
5. For Citus runs, node-0 must currently preload `citus` and node-3/node-4 must be the worker standbys.
6. For vanilla runs, node-0 must be a plain PostgreSQL primary and node-3 must be its standby.

### Build And Install Prerequisites

The setup scripts assume the binaries are already available under `~/pg`. The cluster-specific steps are:

- PostgreSQL: work from the `pg-17-timings` branch, reconfigure the Meson build tree under `build/` with the new `~/pg` prefix, then install that build tree.
- Citus: work from the copied `timings-jason` tree, clean out stale copied build artifacts if needed, rerun `./configure PG_CONFIG=$HOME/pg/bin/pg_config`, rebuild, and reinstall so the generated makefiles and cached metadata point at the current cluster.

If the tree came from another cluster, a clean rebuild is important for the Citus preload path to work reliably on this one.

## Recommended Reproduction Order

A full end-to-end reproduction usually goes in this order:

1. Build and install PostgreSQL and Citus under `~/pg` on all five nodes.
2. Make sure `pg_hba.conf` allows the `10.10.1.0/24` fabric on all nodes, for both normal connections and replication.
3. Restore the Citus topology and verify `CREATE EXTENSION citus;` succeeds on node-0.
4. Run the Citus synchronous-commit sweep.
5. Run the Citus network-interference sweep.
6. Switch node-0/node-3 to the vanilla primary/standby topology.
7. Run the vanilla synchronous-commit sweep.
8. Run the vanilla network-interference sweep.
9. Restore Citus again before leaving the cluster in a reusable state for later Citus runs.

## How The Scripts Fit Together

The repo has four benchmark families:

- Citus synchronous-commit sweep
- vanilla PostgreSQL synchronous-commit sweep
- Citus network-interference sweep
- vanilla PostgreSQL network-interference sweep

Each family has a setup script, a runner, and usually a plotter.

### Citus Synchronous-Commit Sweep

If you just finished the vanilla experiments, restore the Citus cluster first with the Citus interference setup helper, then ensure the distributed pgbench tables exist. The setup helper can also verify the tables and recreate the background sink table if needed.

First initialize or verify the distributed pgbench tables:

```bash
bash scripts/setup_pgbench_citus.sh 32 postgres
```

That helper runs the standard `pgbench -i -s 32` initializer and then distributes the built-in tables with Citus.

Then run the sweep:

```bash
python3 scripts/run_pgbench_sync_commit_bench.py \
  --dbname postgres \
  --duration 5 \
  --max-clients 32 \
  --pgbench-workers 8 \
  --sampling-rate 1.0 \
  --results-dir bench-results-tuned
```

Useful notes:

- `-c` is the actual session concurrency; the runner sweeps it from 1 to `--max-clients`.
- `-j` is capped separately with `--pgbench-workers` so client-side worker threads do not overwhelm the benchmark driver.
- The runner uses `-M prepared`.
- The default modes are `off`, `local`, `on`, `remote_write`, and `remote_apply`.
- The default result directory is `bench-results`, but for the canonical tuned run we keep the output under `bench-results-tuned`.

Plot the CSV afterward with:

```bash
python3 scripts/plot_pgbench_sync_commit_bench.py \
  --csv bench-results-tuned/pgbench-sync-commit-<timestamp>/results.csv
```

### Vanilla PostgreSQL Synchronous-Commit Sweep

Switch node-0/node-3 away from Citus and into the plain primary/standby topology, then initialize a separate `pgbench_vanilla` database:

```bash
python3 scripts/setup_pgbench_vanilla.py \
  --scale 32 \
  --dbname pgbench_vanilla
```

Then run the vanilla sweep:

```bash
python3 scripts/run_pgbench_sync_commit_bench_vanilla.py \
  --dbname pgbench_vanilla \
  --duration 12 \
  --max-clients 32 \
  --pgbench-workers 8 \
  --sampling-rate 0.05 \
  --results-dir bench-results-vanilla
```

Useful notes:

- This run is the plain PostgreSQL counterpart to the Citus sweep.
- It also uses `-M prepared`.
- The default result directory is `bench-results-vanilla`.
- The helper checks that node-3 is streaming from node-0 before it runs.

To compare the tuned Citus and vanilla sweeps side by side:

```bash
python3 scripts/plot_pgbench_sync_commit_comparison.py \
  --citus-csv bench-results-tuned/pgbench-sync-commit-<timestamp>/results.csv \
  --vanilla-csv bench-results-vanilla/pgbench-sync-commit-<timestamp>/results.csv
```

### Citus Network-Interference Sweep

If you are coming back from vanilla experiments, restore the Citus topology first. Then prepare the background sink table and verify the distributed pgbench tables:

```bash
python3 scripts/setup_citus_network_interference.py \
  --pgbench-scale 32 \
  --dbname postgres
```

Then run the interference sweep:

```bash
python3 scripts/run_citus_network_interference_bench.py \
  --dbname postgres \
  --clients 16 \
  --duration 12 \
  --pgbench-workers 8 \
  --sampling-rate 0.05 \
  --background-levels 1 2 4 8 \
  --results-dir bench-results-network-interference
```

Useful notes:

- The foreground workload stays the same; only the background SQL traffic changes.
- Background readers stream `SELECT *`-style traffic from a large distributed source table.
- Background writers stream `COPY` batches into a distributed sink table.
- The background loops run until a shared stop time so the overlap with `pgbench` is guaranteed.
- The default result directory is `bench-results-network-interference`.

To plot one Citus interference CSV:

```bash
python3 scripts/plot_citus_network_interference.py \
  --csv bench-results-network-interference/citus-network-interference-<timestamp>/results.csv
```

To compare the Citus and vanilla interference sweeps:

```bash
python3 scripts/plot_pgbench_network_interference_comparison.py \
  --citus-csv bench-results-network-interference/citus-network-interference-<timestamp>/results.csv \
  --vanilla-csv bench-results-network-interference/vanilla-network-interference-<timestamp>/results.csv
```

### Vanilla PostgreSQL Network-Interference Sweep

Switch node-0/node-3 into the vanilla primary/standby topology and create the local background sink table:

```bash
python3 scripts/setup_pgbench_vanilla_interference.py \
  --scale 32 \
  --dbname pgbench_vanilla
```

Then run the interference sweep:

```bash
python3 scripts/run_pgbench_network_interference_vanilla.py \
  --dbname pgbench_vanilla \
  --clients 16 \
  --duration 16 \
  --pgbench-workers 8 \
  --sampling-rate 0.2 \
  --background-levels 0 1 2 4 8 \
  --results-dir bench-results-network-interference
```

Useful notes:

- `node-1` is used as the generator host for the background traffic.
- The foreground benchmark is still `pgbench` on node-0.
- `background_level = 0` is the no-background baseline.
- The default result directory is `bench-results-network-interference`.


## Switching Between Citus And Vanilla

The scripts intentionally let node-0/node-3 move between the two topologies, but the switch should be explicit so the state is easy to reason about. The order below is the safe way to do it.

### From Citus To Vanilla

1. Finish or stop any Citus benchmark that is still running.
2. Run the vanilla setup script to repurpose node-0 as a primary and node-3 as its standby:

```bash
python3 scripts/setup_pgbench_vanilla.py \
  --scale 32 \
  --dbname pgbench_vanilla
```

3. If you plan to run the vanilla interference benchmark, add the local sink table:

```bash
python3 scripts/setup_pgbench_vanilla_interference.py \
  --scale 32 \
  --dbname pgbench_vanilla
```

### From Vanilla Back To Citus

1. Finish or stop any vanilla benchmark that is still running.
2. Restore node-0 as a Citus coordinator and node-3 as the worker-1 standby:

```bash
python3 scripts/setup_citus_network_interference.py \
  --pgbench-scale 32 \
  --dbname postgres
```

3. If you only need the distributed pgbench tables and not the interference sink table, pass `--skip-pgbench-setup` to avoid reinitializing the tables.
4. If you are starting from a fresh Citus topology, run `scripts/setup_pgbench_citus.sh 32 postgres` afterward when you specifically want to refresh the distributed pgbench tables.

This explicit switch order matters because the vanilla setup disables the Citus preload on node-0, while the Citus setup expects that preload and the worker standbys to be in place.

## Common Output Layout

Each timestamped run directory contains a `results.csv` and the plots for that run.

Examples:

- `bench-results-tuned/pgbench-sync-commit-<timestamp>/results.csv`
- `bench-results-vanilla/pgbench-sync-commit-<timestamp>/results.csv`
- `bench-results-network-interference/citus-network-interference-<timestamp>/results.csv`
- `bench-results-network-interference/vanilla-network-interference-<timestamp>/results.csv`

The plotters write a sibling `plots/` or `comparison-plots/` directory next to the CSV unless you override `--output-dir`.

## Quick Reference

- Use the Citus setup helper before the Citus benchmarks.
- Use the vanilla setup helper before the vanilla benchmarks.
- Use the Citus interference setup helper before the Citus interference run.
- Use the vanilla interference setup helper before the vanilla interference run.
- Keep the three canonical result trees: `bench-results-network-interference`, `bench-results-tuned`, and `bench-results-vanilla`.
