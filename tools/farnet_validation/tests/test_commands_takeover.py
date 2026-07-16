import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from tools.farnet_validation.commands import CommandRunner, CommandSpec
from tools.farnet_validation.evidence import EvidenceStore
from tools.farnet_validation.model import Verdict
from tools.farnet_validation.takeover import capture_takeover, cleanup_takeover, create_takeover


class CommandTakeoverTests(unittest.TestCase):
    def test_dry_run_does_not_launch(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            runner = CommandRunner(EvidenceStore(Path(td) / "e"), False)
            result = runner.run(CommandSpec("no-launch", (sys.executable, "-c", f"open({str(marker)!r},'w').write('x')"), td))
            self.assertEqual(result.verdict, Verdict.PASS); self.assertFalse(marker.exists())

    def test_soft_timeout_preserves_for_takeover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); store = EvidenceStore(root)
            runner = CommandRunner(store, True)
            result = runner.run(CommandSpec("hang", (sys.executable, "-c", "import time; time.sleep(30)"), td,
                                                   timeout_s=0.05))
            self.assertEqual(result.verdict, Verdict.AWAITING_AGENT)
            lease = create_takeover(root, "test", [result.pid], 10)
            capture = capture_takeover(root, lease["token"])
            self.assertTrue(capture.is_file())
            cleaned = cleanup_takeover(root, lease["token"])
            self.assertEqual(cleaned["cleanup_completed"], 1)
            self.assertIsNotNone(runner.reap(result.pid))

    def test_takeover_wrong_token_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); root.mkdir(exist_ok=True)
            lease=create_takeover(root,"test",[],10)
            with self.assertRaises(PermissionError): capture_takeover(root,"wrong")


if __name__ == "__main__": unittest.main()
