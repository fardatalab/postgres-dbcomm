"""Plan atomic upload and invocation of checked-in remote helper files."""

from __future__ import annotations

from pathlib import Path
import re

from .commands import CommandSpec
from .evidence import sha256_file


HELPER_ROOT = Path(__file__).resolve().parent / "remote"


def helper_digest(name: str) -> str:
    path = HELPER_ROOT / name
    if not path.is_file():
        raise ValueError(f"unknown remote helper: {name}")
    return sha256_file(path)


def upload_specs(host: str, run_id: str, names: tuple[str, ...], cwd: str) -> list[CommandSpec]:
    """Use data-only argv; executable script bodies always come from checked-in files."""
    remote = f"/tmp/farnet-validation-{run_id}/helpers"
    specs = [CommandSpec(f"helper-dir-{host}", ("ssh", host, "mkdir", "-m", "0700", "-p", remote),
                         cwd, timeout_s=30, mutates=True)]
    for name in names:
        local = str(HELPER_ROOT / name)
        temporary = f"{remote}/.{name}.upload"
        specs.append(CommandSpec(f"upload-{host}-{name}", ("scp", local, f"{host}:{temporary}"),
                                 cwd, timeout_s=30, mutates=True))
        specs.append(CommandSpec(f"install-{host}-{name}",
                                 ("ssh", host, "install", "-m", "0700", temporary, f"{remote}/{name}"),
                                 cwd, timeout_s=30, mutates=True))
        specs.append(CommandSpec(f"hash-{host}-{name}",
                                 ("ssh", host, "sha256sum", f"{remote}/{name}"), cwd, timeout_s=30))
    return specs


def invoke_spec(host: str, run_id: str, helper: str, args: tuple[str, ...], cwd: str,
                timeout_s: float = 60, mutates: bool = False) -> CommandSpec:
    helper_digest(helper)  # Refuse generated or missing helper names locally.
    remote = f"/tmp/farnet-validation-{run_id}/helpers/{helper}"
    action_arg = args[1] if len(args) > 1 and args[0].endswith(".sh") else (args[0] if args else "")
    action = f"-{re.sub(r'[^A-Za-z0-9_.-]', '_', action_arg)}" if action_arg else ""
    return CommandSpec(f"{host}-{Path(helper).stem}{action}", ("ssh", host, remote, *args), cwd,
                       timeout_s=timeout_s, mutates=mutates)
