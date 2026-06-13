# Homer FixedLoop Motivation Figures

This directory contains reproducible plotting code for the three current Homer
FixedLoop scheduler-motivation figures. The data is manually encoded from the
warmed run bands in `homer_fixed_loop_baseline_plan_v3.md`; the script plots
band midpoints with asymmetric low/high error bars.

Generate the figures from the repository root:

```sh
python3 figures/homer_fixed_loop/plot_homer_fixed_loop_figures.py
```

Generated artifacts:

- `figure1_fixedloop_interference.{png,pdf}`: default FixedLoop foreground
  degradation under background RDMA basebackup, plus basebackup elapsed time.
- `figure2_waitbatch_spin_sweep.{png,pdf}`: WaitBatch `ready_spin_limit` sweep,
  including the `16384` / `8 MiB` diagnostic point for the "too much wait"
  regime.
- `figure3_static_tradeoff_surface.{png,pdf}`: static Exhaustive/Limited
  tradeoff surface for mixed c4 pgbench versus basebackup elapsed time.
- `homer_fixed_loop_figure_data.csv`: flattened data table used by the plots.

Current intended story order:

1. FixedLoop keeps bulk moving but foreground throughput and p99 suffer.
2. Fixed waiting is not a scheduling solution; post-now versus wait/defer is
   itself a scheduling decision.
3. Static FixedLoop choices form a tradeoff surface, so no single static point
   dominates both foreground and bulk behavior.

