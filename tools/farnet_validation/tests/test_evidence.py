import os
from pathlib import Path
import tempfile
import unittest

from tools.farnet_validation.evidence import EvidenceStore, atomic_write, mark_log, read_log_interval


class EvidenceTests(unittest.TestCase):
    def test_atomic_manifest_and_journals(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td))
            store.write_run({"status": "RUNNING"})
            store.event("phase-start", phase="orient")
            self.assertIn("RUNNING", (Path(td) / "run.json").read_text())
            self.assertIn("phase-start", (Path(td) / "events.jsonl").read_text())

    def test_log_interval(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "service.log"
            path.write_text("old\n")
            start = mark_log(path)
            with path.open("a") as out: out.write("candidate\n")
            end = mark_log(path)
            self.assertEqual(read_log_interval(path, start, end), b"candidate\n")

    def test_log_truncation_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "service.log"
            path.write_text("old data\n")
            start = mark_log(path)
            path.write_text("x")
            with self.assertRaises(ValueError): read_log_interval(path, start)

    def test_log_rotation_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "service.log"; path.write_text("old")
            start = mark_log(path)
            path.rename(Path(td) / "rotated"); path.write_text("new")
            with self.assertRaises(ValueError): read_log_interval(path, start)


if __name__ == "__main__": unittest.main()
