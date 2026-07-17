from pathlib import Path
import os
import subprocess
import unittest

from tools.farnet_validation.phases import PhasePlanner, RunConfig, _basebackup_tag, render_plan
from tools.farnet_validation.profiles import PROFILES, expand_profile
from tools.farnet_validation.remote_helpers import HELPER_ROOT


class ProfileTests(unittest.TestCase):
    def test_all_profiles_expand_in_order(self):
        for name in PROFILES:
            phases = expand_profile(name)
            self.assertEqual(len(phases), len(set(phases)))
            self.assertEqual(phases[-1], "report")

    def test_transport_role_order(self):
        phases = expand_profile("transport-acceptance")
        self.assertLess(phases.index("debug-gate"), phases.index("warmup"))
        self.assertLess(phases.index("repeat-3"), phases.index("basebackup"))
        self.assertLess(phases.index("live-final-scan"), phases.index("postgres-orderly-stop"))
        self.assertLess(phases.index("postgres-orderly-stop"), phases.index("host-survivor-cleanup"))
        self.assertLess(phases.index("host-survivor-cleanup"), phases.index("dpu-orderly-stop-wait"))
        self.assertLess(phases.index("dpu-orderly-stop-wait"), phases.index("collect-teardown-logs"))
        self.assertLess(phases.index("collect-teardown-logs"), phases.index("teardown-ledger-scan"))
        self.assertLess(phases.index("teardown-ledger-scan"), phases.index("shared-memory-cleanup"))

    def test_existing_skips_source_and_deploy(self):
        phases = expand_profile("transport-acceptance", "existing")
        self.assertEqual(phases[0], "artifact-receipt-validate")
        self.assertNotIn("source-snapshot", phases)
        self.assertNotIn("dpu-deploy", phases)

    def test_no_profile_downgrade(self):
        with self.assertRaises(ValueError):
            expand_profile("transport-acceptance", "current-source", "build")

    def test_gate_and_basebackup_argv_encode_topology(self):
        config = RunConfig("test", "transport-acceptance", "current-source", "dpus",
                           "/pg", "/citus", "/prefix", "/tmp/evidence")
        plan = render_plan(config)
        commands = [command for phase in plan["phases"] for command in phase["commands"]]
        gate = next(c for c in commands if c["name"] == "debug-gate-gate-client")
        self.assertEqual(gate["argv"][1], "farnet0")
        self.assertIn("--homer-peer-host 10.10.1.201", (HELPER_ROOT / "gate.sh").read_text())
        sender = next(c for c in commands if c["name"] == "basebackup-sender")
        self.assertTrue(any("host=10.10.1.200" in arg for arg in sender["argv"]))

    def test_basebackup_tag_is_stable_positive_int32(self):
        tag = _basebackup_tag("stage3-fresh-20260717T124510Z")
        self.assertEqual(tag, _basebackup_tag("stage3-fresh-20260717T124510Z"))
        self.assertTrue(tag.isdecimal())
        self.assertGreaterEqual(int(tag), 1)
        self.assertLessEqual(int(tag), 2147483647)

        config = RunConfig("alpha-run-id", "transport-acceptance", "current-source", "dpus",
                           "/pg", "/citus", "/prefix", "/tmp/evidence")
        sender = next(spec for spec in PhasePlanner(config).specs("basebackup")
                      if spec.name == "basebackup-sender")
        target = next(arg for arg in sender.argv if arg.startswith("homer:"))
        self.assertIn(f"tag={_basebackup_tag(config.run_id)}", target)

    def test_basebackup_consumer_rejects_bad_tag_before_state_creation(self):
        run_id = f"tag-preflight-test-{os.getpid()}"
        state = Path(f"/tmp/farnet-validation-{run_id}/basebackup-consumer")
        helper = str(HELPER_ROOT / "basebackup_consumer.sh")

        for tag in ("bad-tag", "0", "2147483648", "99999999999"):
            result = subprocess.run(
                ["bash", helper, "start", run_id, "/prefix", "5", "10", tag, "4", "524288"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(state.exists())

    def test_only_peer_postgres_stop_allows_missing_data(self):
        config = RunConfig("test", "transport-acceptance", "current-source", "dpus",
                           "/pg", "/citus", "/data/dbcomm/pg-citus", "/tmp/evidence")
        specs = PhasePlanner(config).specs("clean")
        local_stop = next(spec for spec in specs if spec.name == "postgres-stop")
        peer_stop = next(spec for spec in specs if "postgres_stop" in spec.argv[2])

        self.assertNotIn("--allow-missing-data", local_stop.argv)
        self.assertEqual(peer_stop.argv[-2:], ("/data/dbcomm/pg-citus", "--allow-missing-data"))

    def test_peer_dpu_helper_directory_is_prepared_before_install(self):
        config = RunConfig("test", "transport-acceptance", "current-source", "dpus",
                           "/pg", "/citus", "/data/dbcomm/pg-citus", "/tmp/evidence")
        specs = PhasePlanner(config).specs("orient")
        prepare = next(i for i, spec in enumerate(specs)
                       if spec.name == "farnet0-dpu_dispatch-prepare")
        installs = [i for i, spec in enumerate(specs)
                    if spec.name == "farnet0-dpu_dispatch-install"]

        self.assertTrue(installs)
        self.assertTrue(all(prepare < install for install in installs))

    def test_python_identity_helper_is_uploaded_installed_and_hashed(self):
        config = RunConfig("test", "transport-acceptance", "current-source", "dpus",
                           "/pg", "/citus", "/data/dbcomm/pg-citus", "/tmp/evidence")
        specs = PhasePlanner(config).specs("orient")

        for host in ("dpu", "farnet0"):
            self.assertTrue(any(spec.name == f"upload-{host}-proc_identity.py" for spec in specs))
            self.assertTrue(any(spec.name == f"install-{host}-proc_identity.py" for spec in specs))
            self.assertTrue(any(spec.name == f"hash-{host}-proc_identity.py" for spec in specs))
        self.assertTrue(any(spec.name == "farnet0-dpu_dispatch-install" and
                            spec.argv[-1] == "proc_identity.py" for spec in specs))


if __name__ == "__main__":
    unittest.main()
