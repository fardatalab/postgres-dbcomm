from pathlib import Path
import unittest

from tools.farnet_validation.phases import RunConfig, render_plan
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


if __name__ == "__main__":
    unittest.main()
