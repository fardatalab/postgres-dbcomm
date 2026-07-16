import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.farnet_validation.receipts import EDGE_ORDER, validate_chain
from tools.farnet_validation.sources import capture_snapshot, source_manifest


class SourceReceiptTests(unittest.TestCase):
    def make_repo(self, root):
        subprocess.run(("git", "init", "-q", root), check=True)
        subprocess.run(("git", "-C", root, "config", "user.email", "test@example.invalid"), check=True)
        subprocess.run(("git", "-C", root, "config", "user.name", "Test"), check=True)
        path = Path(root) / "tracked"; path.write_text("one")
        subprocess.run(("git", "-C", root, "add", "tracked"), check=True)
        subprocess.run(("git", "-C", root, "commit", "-qm", "initial"), check=True)

    def test_manifest_hashes_mode_and_content(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_repo(td); first = source_manifest(Path(td))
            (Path(td) / "tracked").chmod(0o755); second = source_manifest(Path(td))
            self.assertNotEqual(first["identity"], second["identity"])

    def test_untracked_must_be_authorized(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_repo(td); (Path(td) / "input.cfg").write_text("x")
            with self.assertRaises(ValueError): source_manifest(Path(td))
            self.assertTrue(source_manifest(Path(td), ("input.cfg",))["identity"])

    def test_snapshot_has_pre_post_identity(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as out:
            self.make_repo(td)
            manifest = capture_snapshot(Path(td), Path(out) / "source.tar")
            self.assertEqual(len(manifest["archive_sha256"]), 64)

    def test_receipt_earliest_output_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "artifact"; artifact.write_text("x")
            import hashlib
            digest = hashlib.sha256(b"x").hexdigest()
            edges=[]; previous="source-id"
            for name in EDGE_ORDER:
                edge={"name":name,"input_identity":previous,"outputs":{str(artifact):digest},
                      "recipe":{"argv":["fixture",name]}}
                previous=hashlib.sha256(json.dumps(edge,sort_keys=True).encode()).hexdigest()
                edge["fingerprint"] = previous
                edges.append(edge)
            artifact.write_text("y")
            valid, edge, _ = validate_chain(edges, "source-id")
            self.assertFalse(valid); self.assertEqual(edge, "source")

    def test_receipt_order_mismatch(self):
        valid, edge, _ = validate_chain([{"name":"citus-build","input_identity":"x","outputs":{}}], "x")
        self.assertFalse(valid); self.assertEqual(edge, "source")

    def test_receipt_prefix_is_not_complete_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "artifact"; artifact.write_text("x")
            import hashlib
            edge = {"name": "source", "input_identity": "source-id",
                    "outputs": {str(artifact): hashlib.sha256(b"x").hexdigest()},
                    "recipe": {"argv": ["fixture"]}}
            edge["fingerprint"] = hashlib.sha256(json.dumps(edge, sort_keys=True).encode()).hexdigest()
            valid, missing, _ = validate_chain([edge], "source-id")
            self.assertFalse(valid); self.assertEqual(missing, "citus-build")


if __name__ == "__main__": unittest.main()
