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


if __name__ == "__main__":
    unittest.main()
