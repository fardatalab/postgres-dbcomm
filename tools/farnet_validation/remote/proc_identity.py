#!/usr/bin/env python3
"""Race-safe /proc classification and exact pidfd signaling for validation helpers."""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import signal
import sys


PF_KTHREAD = 0x00200000


def _stat_identity(proc_dir: Path) -> tuple[str, int, int]:
    """Return (state, flags, starttime) without assuming comm contains no spaces."""
    text = (proc_dir / "stat").read_text()
    try:
        fields = text.rsplit(") ", 1)[1].split()
        return fields[0], int(fields[6]), int(fields[19])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"malformed proc stat path={proc_dir / 'stat'}") from exc


def _fdinfo_pid(pidfd: int) -> int:
    for line in Path(f"/proc/self/fdinfo/{pidfd}").read_text().splitlines():
        if line.startswith("Pid:"):
            return int(line.split(":", 1)[1].strip())
    raise ValueError("pidfd fdinfo has no Pid field")


def classify(pid: int, proc_root: Path) -> int:
    """Classify only the fallback case where both executable reads already failed."""
    proc_dir = proc_root / str(pid)
    if not proc_dir.is_dir():
        print(f"RACED_EXIT pid={pid} stage=classify")
        return 0

    # Production classification pins the snapshot PID while reading its
    # path-based metadata. A fixture proc root has no corresponding kernel
    # task, so deterministic parser tests intentionally omit this guard.
    pidfd: int | None = None
    if proc_root == Path("/proc"):
        try:
            pidfd = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            print(f"RACED_EXIT pid={pid} stage=classify-pidfd-open")
            return 0
        except OSError as exc:
            print(f"UNREADABLE_USER pid={pid} reason=pidfd-open errno={exc.errno}")
            return 2

    try:
        try:
            if pidfd is not None and _fdinfo_pid(pidfd) != pid:
                print(f"RACED_EXIT pid={pid} stage=classify-pre-read")
                return 0
            state, flags, _starttime = _stat_identity(proc_dir)
            with (proc_dir / "cmdline").open("rb", buffering=0) as cmdline:
                first_byte = cmdline.read(1)
        except FileNotFoundError:
            if not proc_dir.is_dir():
                print(f"RACED_EXIT pid={pid} stage=classify-read")
                return 0
            print(f"UNREADABLE_USER pid={pid} reason=proc-race-ambiguous")
            return 2
        except (OSError, ValueError) as exc:
            print(f"UNREADABLE_USER pid={pid} reason=proc-read detail={type(exc).__name__}")
            return 2

        try:
            if pidfd is not None and _fdinfo_pid(pidfd) != pid:
                print(f"RACED_EXIT pid={pid} stage=classify-post-read")
                return 0
        except (OSError, ValueError) as exc:
            print(f"UNREADABLE_USER pid={pid} reason=pidfd-recheck detail={type(exc).__name__}")
            return 2

        if state == "Z":
            print(f"ZOMBIE_NO_EXE pid={pid}")
            return 0
        if flags & PF_KTHREAD:
            print(f"KERNEL_NO_EXE pid={pid}")
            return 0
        reason = "exe" if first_byte else "empty-cmdline-not-kthread"
        print(f"UNREADABLE_USER pid={pid} reason={reason}")
        return 2
    finally:
        if pidfd is not None:
            os.close(pidfd)


def pidfd_signal(pid: int, expected_start: int, expected_exe: str) -> int:
    """Pin, revalidate, and SIGKILL exactly the process identity selected by cleanup."""
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        print(f"PIDFD_RACED_EXIT pid={pid} stage=open")
        return 0
    except OSError as exc:
        print(f"PIDFD_OPEN_FAILED pid={pid} errno={exc.errno}")
        return 2

    try:
        if _fdinfo_pid(pidfd) != pid:
            print(f"PIDFD_RACED_EXIT pid={pid} stage=pre-identity")
            return 0

        proc_dir = Path("/proc") / str(pid)
        try:
            _state, _flags, observed_start = _stat_identity(proc_dir)
            observed_exe = os.path.realpath(proc_dir / "exe")
        except FileNotFoundError:
            print(f"PIDFD_RACED_EXIT pid={pid} stage=identity")
            return 0
        except (OSError, ValueError) as exc:
            print(f"PIDFD_IDENTITY_UNREADABLE pid={pid} detail={type(exc).__name__}")
            return 2

        # Recheck the pinned handle after the path-based reads. If the pinned
        # task exited and its numeric PID was reused, /proc/PID now names a
        # different task but fdinfo reports the original pidfd as dead.
        if _fdinfo_pid(pidfd) != pid:
            print(f"PIDFD_RACED_EXIT pid={pid} stage=post-identity")
            return 0
        if observed_start != expected_start or observed_exe != expected_exe:
            print(
                "PIDFD_IDENTITY_MISMATCH "
                f"pid={pid} expected_start={expected_start} observed_start={observed_start} "
                f"expected_exe={expected_exe} observed_exe={observed_exe}"
            )
            return 2

        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            print(f"PIDFD_RACED_EXIT pid={pid} stage=signal")
            return 0
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                print(f"PIDFD_RACED_EXIT pid={pid} stage=signal")
                return 0
            print(f"PIDFD_SIGNAL_FAILED pid={pid} errno={exc.errno}")
            return 2
        print(f"PIDFD_SIGNALLED pid={pid} start={observed_start} exe={observed_exe} signal=KILL")
        return 0
    finally:
        os.close(pidfd)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    classify_parser = subparsers.add_parser("classify")
    classify_parser.add_argument("pid", type=int)
    classify_parser.add_argument("--proc-root", type=Path, default=Path("/proc"))

    signal_parser = subparsers.add_parser("signal")
    signal_parser.add_argument("pid", type=int)
    signal_parser.add_argument("expected_start", type=int)
    signal_parser.add_argument("expected_exe")

    args = parser.parse_args()
    if args.action == "classify":
        return classify(args.pid, args.proc_root)
    return pidfd_signal(args.pid, args.expected_start, args.expected_exe)


if __name__ == "__main__":
    sys.exit(main())
