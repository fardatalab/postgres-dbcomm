from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools.farnet_validation.profiles import PROFILES
from tools.farnet_validation.validate import main


class CliTests(unittest.TestCase):
    def test_plan_and_dry_run_every_profile_without_launch(self):
        for profile in PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
                common = ["--profile", profile, "--run-id", f"test-{profile}",
                          "--evidence-dir", td]
                if profile == "dma-bridge": common += ["--baseline-run", "/nonexistent/baseline.json"]
                self.assertEqual(main(["plan", *common]), 0)
                self.assertEqual(main(["run", *common]), 0)
                self.assertTrue((Path(td) / "run.json").is_file())
                self.assertNotIn("execute", (Path(td) / "events.jsonl").read_text())

    def test_live_execution_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["run", "--profile", "build", "--run-id", "blocked-live",
                                   "--evidence-dir", td, "--execute"]), 3)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_live_resume_is_fail_closed_before_reading_state(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["resume", "/nonexistent/run", "--execute"]), 3)

    def test_takeover_cleanup_is_fail_closed_before_reading_state(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["takeover", "/nonexistent/run", "--token", "x",
                                   "--cleanup", "--execute"]), 3)

    def test_remote_shell_tokens_are_rejected(self):
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["plan", "--profile", "build", "--run-id", "bad;touch-pwned",
                                   "--evidence-dir", td]), 3)

    def test_summary_cannot_pass_missing_role_logs(self):
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
            root = Path(td)
            (root / "run.json").write_text(json.dumps({"run_id": "r", "profile": "transport-acceptance",
                                                        "status": "PASS", "options": {}}))
            (root / "commands.jsonl").write_text(json.dumps({"status": "EXITED", "returncode": 0}) + "\n")
            self.assertEqual(main(["summarize", td]), 2)
            self.assertIn("INCONCLUSIVE", (root / "summary.md").read_text())


if __name__ == "__main__": unittest.main()
