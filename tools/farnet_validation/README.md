# Farnet validation runner (fail-closed checkpoint)

This directory is the implementation companion to
[`docs/kb/operations/farnet_validation_runner_plan.md`](../../docs/kb/operations/farnet_validation_runner_plan.md).
It currently supports profile expansion, non-executing dry runs, receipt/log parsing, structured verdict summaries,
and process-local tests.

It is **not yet a live validation authority**. `run --execute`, `resume --execute`, and takeover cleanup all reject
the invocation while `LIVE_EXECUTION_READY` is false. Continue to use `AGENTS.md` and the `farnet-validation` skill
for real gate/basebackup validation until the KB status records a known-green runner acceptance.

Safe development commands:

```sh
python3 tools/farnet_validation/validate.py plan \
  --profile transport-acceptance --run-id inspect

python3 tools/farnet_validation/validate.py run \
  --profile transport-acceptance --run-id dry-run \
  --evidence-dir /tmp/farnet-validation-dry-run

python3 -m unittest discover -s tools/farnet_validation/tests -v
```

The second command records the planned state machine but launches no build, SSH, service, or workload command.
Use a fresh evidence directory for every invocation.
