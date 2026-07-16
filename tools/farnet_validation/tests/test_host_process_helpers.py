from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from tools.farnet_validation.remote_helpers import HELPER_ROOT


class HostProcessHelperTests(unittest.TestCase):
    def _prefix_fixture(self, root: Path) -> tuple[Path, Path]:
        physical_parent = root / "physical-dbcomm"
        physical_prefix = physical_parent / "pg-citus"
        (physical_prefix / "bin").mkdir(parents=True)
        logical_parent = root / "logical-dbcomm"
        logical_parent.symlink_to(physical_parent, target_is_directory=True)
        return logical_parent / "pg-citus", physical_prefix

    def test_probe_matches_process_through_logical_parent_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            logical_prefix, physical_prefix = self._prefix_fixture(Path(temporary))
            executable = physical_prefix / "bin" / "pgbench-symlink-test"
            shutil.copy2("/bin/sleep", executable)
            process = subprocess.Popen([str(executable), "30"])
            try:
                result = subprocess.run(
                    ["bash", str(HELPER_ROOT / "host_process_probe.sh"), str(logical_prefix), "stopped"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertIn(
                    f"PREFIX_IDENTITY logical={logical_prefix} canonical={physical_prefix}", result.stdout
                )
                self.assertIn(f"FORBIDDEN_PROCESS pid={process.pid} ", result.stdout)
                self.assertIn(f"exe={executable} kind=client-or-host-service", result.stdout)
                self.assertEqual(result.returncode, 2)
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_cleanup_matches_process_through_logical_parent_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logical_prefix, physical_prefix = self._prefix_fixture(root)
            executable = physical_prefix / "bin" / "pgbench-symlink-cleanup-test"
            shutil.copy2("/bin/sleep", executable)

            # Keep the helper self-contained: its established production shape
            # uses sudo even for a caller-owned process.
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text("#!/bin/sh\n[ \"$1\" = -n ] && shift\nexec \"$@\"\n")
            fake_sudo.chmod(0o700)
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

            process = subprocess.Popen([str(executable), "30"])
            try:
                result = subprocess.run(
                    ["bash", str(HELPER_ROOT / "host_process_cleanup.sh"), str(logical_prefix)],
                    text=True,
                    capture_output=True,
                    check=False,
                    env=env,
                )
                process.wait(timeout=5)
                self.assertIn(
                    f"PREFIX_IDENTITY logical={logical_prefix} canonical={physical_prefix}", result.stdout
                )
                self.assertEqual(process.returncode, -9)
                # An unrelated unreadable user process may still make the
                # helper fail closed after it reaps this exact fixture.
                self.assertIn(result.returncode, (0, 2), result.stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_probe_and_cleanup_ignore_sibling_physical_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logical_prefix, _physical_prefix = self._prefix_fixture(root)
            sibling_prefix = root / "sibling-dbcomm" / "pg-citus"
            (sibling_prefix / "bin").mkdir(parents=True)
            executable = sibling_prefix / "bin" / "pgbench-wrong-tree-test"
            shutil.copy2("/bin/sleep", executable)

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text("#!/bin/sh\n[ \"$1\" = -n ] && shift\nexec \"$@\"\n")
            fake_sudo.chmod(0o700)
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

            process = subprocess.Popen([str(executable), "30"])
            try:
                probe = subprocess.run(
                    ["bash", str(HELPER_ROOT / "host_process_probe.sh"), str(logical_prefix), "stopped"],
                    text=True,
                    capture_output=True,
                    check=False,
                    env=env,
                )
                cleanup = subprocess.run(
                    ["bash", str(HELPER_ROOT / "host_process_cleanup.sh"), str(logical_prefix)],
                    text=True,
                    capture_output=True,
                    check=False,
                    env=env,
                )

                self.assertNotIn(f"pid={process.pid} ", probe.stdout)
                self.assertNotIn(f"pid={process.pid} ", cleanup.stdout)
                self.assertIsNone(process.poll())
                # Ambient unreadable user processes may make either helper
                # fail closed, but the sibling executable must never match.
                self.assertIn(probe.returncode, (0, 2), probe.stderr)
                self.assertIn(cleanup.returncode, (0, 2), cleanup.stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_probe_rejects_canonical_prefix_identity_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logical_prefix, _physical_prefix = self._prefix_fixture(root)
            wrong_expected = root / "different-physical-prefix"
            result = subprocess.run(
                [
                    "bash",
                    str(HELPER_ROOT / "host_process_probe.sh"),
                    str(logical_prefix),
                    "stopped",
                    str(wrong_expected),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 3)
            self.assertIn("installed prefix identity changed", result.stderr)

    def test_probe_fails_before_scan_when_prefix_cannot_be_canonicalized(self):
        missing = Path(tempfile.gettempdir()) / "farnet-validation-missing-prefix"
        result = subprocess.run(
            ["bash", str(HELPER_ROOT / "host_process_probe.sh"), str(missing), "stopped"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("could not canonicalize installed prefix", result.stderr)

    def test_postgres_stop_requires_explicit_missing_data_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary) / "pg-citus"
            (prefix / "bin").mkdir(parents=True)
            shutil.copy2("/bin/true", prefix / "bin" / "pg_ctl")
            helper = str(HELPER_ROOT / "postgres_stop.sh")

            required = subprocess.run(
                ["bash", helper, str(prefix)], text=True, capture_output=True, check=False
            )
            allowed = subprocess.run(
                ["bash", helper, str(prefix), "--allow-missing-data"],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(required.returncode, 2)
            self.assertIn("capability=require-data", required.stderr)
            self.assertEqual(allowed.returncode, 0)
            self.assertIn("missing_data=1 capability=allow-missing-data", allowed.stdout)


if __name__ == "__main__":
    unittest.main()
