from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from tools.farnet_validation.remote_helpers import HELPER_ROOT


HELPER = HELPER_ROOT / "proc_identity.py"
PF_KTHREAD = 0x00200000


def _write_stat(proc_dir: Path, pid: int, state: str, flags: int, starttime: int) -> None:
    # Fields begin at proc stat field 3 after the closing `) `. The helper
    # consumes state=index 0, flags=index 6, and starttime=index 19.
    fields = ["0"] * 50
    fields[0] = state
    fields[6] = str(flags)
    fields[19] = str(starttime)
    (proc_dir / "stat").write_text(f"{pid} (fixture comm) {' '.join(fields)}\n")


class ProcIdentityTests(unittest.TestCase):
    def _classify(self, root: Path, pid: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(HELPER), "classify", str(pid), "--proc-root", str(root)],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_classify_vanished_snapshot_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self._classify(Path(temporary), 1234)
            self.assertEqual(result.returncode, 0)
            self.assertIn("RACED_EXIT pid=1234", result.stdout)

    def test_classify_kernel_and_zombie_without_exe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for pid, state, flags, expected in (
                (11, "S", PF_KTHREAD, "KERNEL_NO_EXE"),
                (12, "Z", 0, "ZOMBIE_NO_EXE"),
            ):
                proc_dir = root / str(pid)
                proc_dir.mkdir()
                _write_stat(proc_dir, pid, state, flags, 99)
                (proc_dir / "cmdline").write_bytes(b"")
                result = self._classify(root, pid)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"{expected} pid={pid}", result.stdout)

    def test_classify_live_user_is_unreadable_even_with_empty_cmdline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for pid, cmdline, reason in (
                (21, b"worker\0--flag\0", "reason=exe"),
                (22, b"", "reason=empty-cmdline-not-kthread"),
            ):
                proc_dir = root / str(pid)
                proc_dir.mkdir()
                _write_stat(proc_dir, pid, "S", 0, 100)
                (proc_dir / "cmdline").write_bytes(cmdline)
                result = self._classify(root, pid)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"UNREADABLE_USER pid={pid}", result.stdout)
                self.assertIn(reason, result.stdout)

    def test_scan_aggregates_benign_tasks_and_keeps_executable_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for pid, state, flags in ((11, "S", PF_KTHREAD), (12, "Z", 0)):
                proc_dir = root / str(pid)
                proc_dir.mkdir()
                _write_stat(proc_dir, pid, state, flags, 90 + pid)
                (proc_dir / "cmdline").write_bytes(b"")

            proc_dir = root / "13"
            proc_dir.mkdir()
            _write_stat(proc_dir, 13, "S", 0, 103)
            (proc_dir / "exe").symlink_to("/bin/true")
            (proc_dir / "cmdline").write_bytes(b"/bin/true\0")

            result = subprocess.run(
                ["python3", str(HELPER), "scan", "--proc-root", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"SCAN_EXE\t13\t103\t{os.path.realpath('/bin/true')}\t0", result.stdout)
            self.assertIn("SCAN_SUMMARY\t1\t1\t1\t0\t0", result.stdout)
            self.assertNotIn("SCAN_BENIGN", result.stdout)
            self.assertNotIn("KERNEL_NO_EXE", result.stdout)

            verbose = subprocess.run(
                ["python3", str(HELPER), "scan", "--proc-root", str(root), "--verbose-benign"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(verbose.returncode, 0, verbose.stdout + verbose.stderr)
            self.assertIn("SCAN_BENIGN\tkernel-no-exe\t11\tclassified", verbose.stdout)
            self.assertIn("SCAN_BENIGN\tzombie-no-exe\t12\tclassified", verbose.stdout)

    def test_scan_fails_closed_for_live_user_without_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc_dir = root / "21"
            proc_dir.mkdir()
            _write_stat(proc_dir, 21, "S", 0, 121)
            (proc_dir / "cmdline").write_bytes(b"worker\0")

            result = subprocess.run(
                ["python3", str(HELPER), "scan", "--proc-root", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("SCAN_UNREADABLE\t21\texe-missing", result.stdout)
            self.assertIn("SCAN_SUMMARY\t0\t0\t0\t0\t1", result.stdout)

    def test_scan_fixture_encodes_remote_exec_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc_dir = root / "31"
            proc_dir.mkdir()
            _write_stat(proc_dir, 31, "S", 0, 131)
            (proc_dir / "exe").symlink_to("/bin/true")
            (proc_dir / "cmdline").write_bytes(b"postgres: remote exec backend\0")

            result = subprocess.run(
                ["python3", str(HELPER), "scan", "--proc-root", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"SCAN_EXE\t31\t131\t{os.path.realpath('/bin/true')}\t1", result.stdout)

    def test_scan_carries_remote_exec_role_under_production_pidfd_snapshot(self):
        process = subprocess.Popen(
            ["bash", "-c", 'exec -a "postgres: remote exec backend" /bin/sleep 30']
        )
        try:
            result = subprocess.run(
                ["python3", str(HELPER), "scan"],
                text=True,
                capture_output=True,
                check=False,
            )
            expected = f"SCAN_EXE\t{process.pid}\t"
            matching = [line for line in result.stdout.splitlines() if line.startswith(expected)]
            self.assertEqual(len(matching), 1, result.stdout + result.stderr)
            self.assertTrue(matching[0].endswith("\t1"), matching[0])
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_scan_preserves_deleted_executable_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc_dir = root / "41"
            proc_dir.mkdir()
            _write_stat(proc_dir, 41, "S", 0, 141)
            (proc_dir / "exe").symlink_to("/tmp/deleted-fixture (deleted)")
            (proc_dir / "cmdline").write_bytes(b"fixture\0")

            result = subprocess.run(
                ["python3", str(HELPER), "scan", "--proc-root", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SCAN_EXE\t41\t141\t/tmp/deleted-fixture (deleted)\t0", result.stdout)

    def test_classify_real_zombie_through_production_pidfd_path(self):
        process = subprocess.Popen(["/bin/true"])
        try:
            for _attempt in range(100):
                stat_path = Path(f"/proc/{process.pid}/stat")
                if stat_path.exists() and stat_path.read_text().rsplit(") ", 1)[1].startswith("Z "):
                    break
                time.sleep(0.01)
            else:
                self.fail("fixture process did not become a zombie")

            result = subprocess.run(
                ["python3", str(HELPER), "classify", str(process.pid)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"ZOMBIE_NO_EXE pid={process.pid}", result.stdout)
        finally:
            process.wait(timeout=5)

    def test_pidfd_signal_kills_only_matching_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "sleep-pidfd-test"
            shutil.copy2("/bin/sleep", executable)
            process = subprocess.Popen([str(executable), "30"])
            try:
                stat_text = Path(f"/proc/{process.pid}/stat").read_text()
                fields = stat_text.rsplit(") ", 1)[1].split()
                starttime = int(fields[19])
                expected_exe = os.path.realpath(f"/proc/{process.pid}/exe")
                result = subprocess.run(
                    ["python3", str(HELPER), "signal", str(process.pid), str(starttime), expected_exe],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                process.wait(timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PIDFD_SIGNALLED", result.stdout)
                self.assertEqual(process.returncode, -9)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_pidfd_signal_rejects_identity_mismatch(self):
        process = subprocess.Popen(["/bin/sleep", "30"])
        try:
            stat_text = Path(f"/proc/{process.pid}/stat").read_text()
            fields = stat_text.rsplit(") ", 1)[1].split()
            starttime = int(fields[19])
            expected_exe = os.path.realpath(f"/proc/{process.pid}/exe")
            result = subprocess.run(
                ["python3", str(HELPER), "signal", str(process.pid), str(starttime + 1), expected_exe],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("PIDFD_IDENTITY_MISMATCH", result.stdout)
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_pidfd_digest_hashes_only_matching_pinned_identity(self):
        process = subprocess.Popen(["/bin/sleep", "30"])
        try:
            stat_text = Path(f"/proc/{process.pid}/stat").read_text()
            starttime = int(stat_text.rsplit(") ", 1)[1].split()[19])
            expected_exe = os.path.realpath(f"/proc/{process.pid}/exe")
            result = subprocess.run(
                ["python3", str(HELPER), "digest", str(process.pid), str(starttime), expected_exe],
                text=True,
                capture_output=True,
                check=False,
            )
            fields = result.stdout.strip().split("\t")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(fields[:3], ["PIDFD_DIGEST", str(process.pid), str(starttime)])
            self.assertRegex(fields[3], r"^[0-9a-f]{64}$")
            self.assertEqual(fields[4], expected_exe)
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_pidfd_digest_rejects_identity_mismatch(self):
        process = subprocess.Popen(["/bin/sleep", "30"])
        try:
            stat_text = Path(f"/proc/{process.pid}/stat").read_text()
            starttime = int(stat_text.rsplit(") ", 1)[1].split()[19])
            expected_exe = os.path.realpath(f"/proc/{process.pid}/exe")
            result = subprocess.run(
                [
                    "python3",
                    str(HELPER),
                    "digest",
                    str(process.pid),
                    str(starttime + 1),
                    expected_exe,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("PIDFD_DIGEST_IDENTITY_MISMATCH", result.stdout)
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
