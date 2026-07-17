#!/usr/bin/env python3
"""Race-safe /proc classification and exact pidfd signaling for validation helpers."""

from __future__ import annotations

import argparse
import errno
import hashlib
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


def _exe_identity(proc_dir: Path) -> str:
    """Return the same canonical executable identity used by scan and rechecks."""
    target = os.readlink(proc_dir / "exe")
    return target if target.endswith(" (deleted)") else os.path.realpath(target)


def _revalidate_pinned_identity(
    pidfd: int, pid: int, expected_start: int, expected_exe: str
) -> tuple[int, str]:
    """Revalidate path metadata while the original task remains pidfd-pinned."""
    if _fdinfo_pid(pidfd) != pid:
        raise ProcessLookupError
    proc_dir = Path("/proc") / str(pid)
    _state, _flags, observed_start = _stat_identity(proc_dir)
    observed_exe = _exe_identity(proc_dir)
    if _fdinfo_pid(pidfd) != pid:
        raise ProcessLookupError
    if observed_start != expected_start or observed_exe != expected_exe:
        raise RuntimeError(
            f"expected_start={expected_start} observed_start={observed_start} "
            f"expected_exe={expected_exe} observed_exe={observed_exe}"
        )
    return observed_start, observed_exe


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


def _scan_benign(verbose: bool, kind: str, pid: int, stage: str) -> None:
    """Emit an optional per-PID benign record; summaries remain unconditional."""
    if verbose:
        print(f"SCAN_BENIGN\t{kind}\t{pid}\t{stage}")


def scan(proc_root: Path, verbose: bool = False) -> int:
    """Resolve a complete /proc snapshot in one process with bounded output.

    Every production PID is pinned while its start time and executable/no-exe
    classification are read.  Benign kernel, zombie, and raced-exit cases are
    aggregated by default; resolved executables and unreadable live user
    identities remain per-PID records for the calling policy helper.
    """
    counts = {"resolved": 0, "kernel": 0, "zombie": 0, "raced": 0, "unreadable": 0}
    try:
        pids = sorted(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdecimal())
    except OSError as exc:
        print(f"SCAN_ERROR\tproc-list\t{type(exc).__name__}")
        return 2

    for pid in pids:
        proc_dir = proc_root / str(pid)
        pidfd: int | None = None
        if proc_root == Path("/proc"):
            try:
                pidfd = os.pidfd_open(pid, 0)
            except ProcessLookupError:
                counts["raced"] += 1
                _scan_benign(verbose, "raced-exit", pid, "pidfd-open")
                continue
            except OSError as exc:
                counts["unreadable"] += 1
                print(f"SCAN_UNREADABLE\t{pid}\tpidfd-open-errno-{exc.errno}")
                continue

        try:
            try:
                if pidfd is not None and _fdinfo_pid(pidfd) != pid:
                    counts["raced"] += 1
                    _scan_benign(verbose, "raced-exit", pid, "pre-read")
                    continue
                state, flags, starttime = _stat_identity(proc_dir)
            except FileNotFoundError:
                counts["raced"] += 1
                _scan_benign(verbose, "raced-exit", pid, "stat-read")
                continue
            except (OSError, ValueError) as exc:
                counts["unreadable"] += 1
                print(f"SCAN_UNREADABLE\t{pid}\tstat-{type(exc).__name__}")
                continue

            try:
                exe_target = os.readlink(proc_dir / "exe")
            except FileNotFoundError:
                # A stable no-exe task still needs direct kernel/zombie proof.
                try:
                    with (proc_dir / "cmdline").open("rb", buffering=0) as cmdline:
                        first_byte = cmdline.read(1)
                except FileNotFoundError:
                    if not proc_dir.is_dir():
                        counts["raced"] += 1
                        _scan_benign(verbose, "raced-exit", pid, "cmdline-read")
                        continue
                    counts["unreadable"] += 1
                    print(f"SCAN_UNREADABLE\t{pid}\tproc-race-ambiguous")
                    continue
                except OSError as exc:
                    counts["unreadable"] += 1
                    print(f"SCAN_UNREADABLE\t{pid}\tcmdline-{type(exc).__name__}")
                    continue

                if state == "Z":
                    counts["zombie"] += 1
                    _scan_benign(verbose, "zombie-no-exe", pid, "classified")
                    continue
                if flags & PF_KTHREAD:
                    counts["kernel"] += 1
                    _scan_benign(verbose, "kernel-no-exe", pid, "classified")
                    continue
                counts["unreadable"] += 1
                reason = "exe-missing" if first_byte else "empty-cmdline-not-kthread"
                print(f"SCAN_UNREADABLE\t{pid}\t{reason}")
                continue
            except OSError as exc:
                counts["unreadable"] += 1
                print(f"SCAN_UNREADABLE\t{pid}\texe-read-{type(exc).__name__}")
                continue

            try:
                if pidfd is not None and _fdinfo_pid(pidfd) != pid:
                    counts["raced"] += 1
                    _scan_benign(verbose, "raced-exit", pid, "post-read")
                    continue
            except (OSError, ValueError) as exc:
                counts["unreadable"] += 1
                print(f"SCAN_UNREADABLE\t{pid}\tpidfd-recheck-{type(exc).__name__}")
                continue

            # PostgreSQL rewrites argv with its backend role.  Carry only the
            # semantic bit needed by the host policy rather than emitting
            # arbitrary command lines. A missing/empty read is explicit so a
            # project postgres cannot silently look like an ordinary backend.
            try:
                cmdline = (proc_dir / "cmdline").read_bytes()[:4096]
                remote_exec_backend = -1 if not cmdline else int(b"remote exec backend" in cmdline)
            except OSError:
                remote_exec_backend = -1

            try:
                if pidfd is not None and _fdinfo_pid(pidfd) != pid:
                    counts["raced"] += 1
                    _scan_benign(verbose, "raced-exit", pid, "post-cmdline")
                    continue
            except (OSError, ValueError) as exc:
                counts["unreadable"] += 1
                print(f"SCAN_UNREADABLE\t{pid}\tpidfd-cmdline-recheck-{type(exc).__name__}")
                continue

            # Preserve the kernel's explicit deleted suffix; otherwise make
            # the executable identity canonical like readlink -f did.
            exe = exe_target if exe_target.endswith(" (deleted)") else os.path.realpath(exe_target)
            print(f"SCAN_EXE\t{pid}\t{starttime}\t{exe}\t{remote_exec_backend}")
            counts["resolved"] += 1
        finally:
            if pidfd is not None:
                os.close(pidfd)

    print(
        "SCAN_SUMMARY\t"
        f"{counts['resolved']}\t{counts['kernel']}\t{counts['zombie']}\t"
        f"{counts['raced']}\t{counts['unreadable']}"
    )
    return 2 if counts["unreadable"] else 0


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

        try:
            observed_start, observed_exe = _revalidate_pinned_identity(
                pidfd, pid, expected_start, expected_exe
            )
        except (FileNotFoundError, ProcessLookupError):
            print(f"PIDFD_RACED_EXIT pid={pid} stage=identity")
            return 0
        except RuntimeError as exc:
            print(f"PIDFD_IDENTITY_MISMATCH pid={pid} {exc}")
            return 2
        except (OSError, ValueError) as exc:
            print(f"PIDFD_IDENTITY_UNREADABLE pid={pid} detail={type(exc).__name__}")
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


def pidfd_digest(pid: int, expected_start: int, expected_exe: str) -> int:
    """Hash one matched executable between two pinned identity checks.

    Full-snapshot scanning stays batched. This colder action runs only for a
    DPU service that can affect the validation verdict, so it closes the
    scan-to-digest PID-reuse window without restoring per-PID subprocesses.
    """
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        print(f"PIDFD_DIGEST_RACED_EXIT pid={pid} stage=open")
        return 2
    except OSError as exc:
        print(f"PIDFD_DIGEST_OPEN_FAILED pid={pid} errno={exc.errno}")
        return 2

    try:
        try:
            observed_start, observed_exe = _revalidate_pinned_identity(
                pidfd, pid, expected_start, expected_exe
            )
            digest = hashlib.sha256()
            with (Path("/proc") / str(pid) / "exe").open("rb", buffering=0) as executable:
                while chunk := executable.read(1024 * 1024):
                    digest.update(chunk)
            _revalidate_pinned_identity(pidfd, pid, expected_start, expected_exe)
        except (FileNotFoundError, ProcessLookupError):
            print(f"PIDFD_DIGEST_RACED_EXIT pid={pid} stage=identity-or-read")
            return 2
        except RuntimeError as exc:
            print(f"PIDFD_DIGEST_IDENTITY_MISMATCH pid={pid} {exc}")
            return 2
        except (OSError, ValueError) as exc:
            print(f"PIDFD_DIGEST_UNREADABLE pid={pid} detail={type(exc).__name__}")
            return 2

        print(
            f"PIDFD_DIGEST\t{pid}\t{observed_start}\t{digest.hexdigest()}\t{observed_exe}"
        )
        return 0
    finally:
        os.close(pidfd)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    classify_parser = subparsers.add_parser("classify")
    classify_parser.add_argument("pid", type=int)
    classify_parser.add_argument("--proc-root", type=Path, default=Path("/proc"))

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    scan_parser.add_argument("--verbose-benign", action="store_true")

    signal_parser = subparsers.add_parser("signal")
    signal_parser.add_argument("pid", type=int)
    signal_parser.add_argument("expected_start", type=int)
    signal_parser.add_argument("expected_exe")

    digest_parser = subparsers.add_parser("digest")
    digest_parser.add_argument("pid", type=int)
    digest_parser.add_argument("expected_start", type=int)
    digest_parser.add_argument("expected_exe")

    args = parser.parse_args()
    if args.action == "classify":
        return classify(args.pid, args.proc_root)
    if args.action == "scan":
        return scan(args.proc_root, args.verbose_benign)
    if args.action == "digest":
        return pidfd_digest(args.pid, args.expected_start, args.expected_exe)
    return pidfd_signal(args.pid, args.expected_start, args.expected_exe)


if __name__ == "__main__":
    sys.exit(main())
