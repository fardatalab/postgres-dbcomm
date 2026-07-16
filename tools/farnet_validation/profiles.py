"""Explicit validation profiles and their ordered phase expansions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Preparation(IntEnum):
    NONE = 0
    BUILD = 1
    INSTALL = 2
    PEER = 3
    DPUS = 4


ARTIFACT_PHASES = (
    "orient", "ownership", "source-snapshot", "citus-config-build",
    "citus-install", "postgres-config-relink", "postgres-install",
    "peer-sync", "dpu-deploy",
)
RUNTIME_PREFIX = ("clean", "start", "post-start-preflight", "prepare")
GATE_DEBUG = ("debug-gate", "alarm-scan")
GATE_ACCEPT = (
    "pre-warmup-preflight", "warmup",
    "pre-repeat-1-preflight", "repeat-1",
    "pre-repeat-2-preflight", "repeat-2",
    "pre-repeat-3-preflight", "repeat-3",
)
BASEBACKUP = ("pre-basebackup-preflight", "basebackup")
TEARDOWN = (
    "live-final-scan", "postgres-orderly-stop", "host-survivor-cleanup",
    "dpu-orderly-stop-wait", "collect-teardown-logs",
    "teardown-ledger-scan", "shared-memory-cleanup",
    "final-process-listener-preflight", "report",
)


@dataclass(frozen=True)
class Profile:
    name: str
    phases: tuple[str, ...]
    minimum_preparation: Preparation
    requires_baseline: bool = False


PROFILES = {
    "build": Profile("build", ARTIFACT_PHASES[:4] + ("report",), Preparation.BUILD),
    "gate-smoke": Profile("gate-smoke", ARTIFACT_PHASES + RUNTIME_PREFIX + GATE_DEBUG + TEARDOWN,
                          Preparation.DPUS),
    "gate-acceptance": Profile("gate-acceptance", ARTIFACT_PHASES + RUNTIME_PREFIX + GATE_DEBUG +
                               GATE_ACCEPT + TEARDOWN, Preparation.DPUS),
    "transport-acceptance": Profile("transport-acceptance", ARTIFACT_PHASES + RUNTIME_PREFIX + GATE_DEBUG +
                                    GATE_ACCEPT + BASEBACKUP + TEARDOWN, Preparation.DPUS),
    "dma-bridge": Profile("dma-bridge", ARTIFACT_PHASES + ("validate-baseline-smoke", "candidate-dpu-tcp-smoke") +
                          RUNTIME_PREFIX + GATE_DEBUG + GATE_ACCEPT + BASEBACKUP + TEARDOWN,
                          Preparation.DPUS, True),
}


def expand_profile(name: str, artifact_mode: str = "current-source",
                   prepare_through: str | None = None) -> tuple[str, ...]:
    if name not in PROFILES:
        raise ValueError(f"unknown profile: {name}")
    if artifact_mode not in {"current-source", "existing"}:
        raise ValueError(f"unknown artifact mode: {artifact_mode}")
    profile = PROFILES[name]
    selected = Preparation[prepare_through.upper()] if prepare_through else profile.minimum_preparation
    if artifact_mode == "current-source" and selected < profile.minimum_preparation:
        raise ValueError(f"{name} requires preparation through {profile.minimum_preparation.name.lower()}")
    if artifact_mode == "existing":
        # Existing mode validates a receipt but never makes a current-worktree claim.
        return tuple("artifact-receipt-validate" if p == "orient" else p
                     for p in profile.phases if p not in ARTIFACT_PHASES[1:])
    edge_last = {Preparation.BUILD: "citus-config-build", Preparation.INSTALL: "postgres-install",
                 Preparation.PEER: "peer-sync", Preparation.DPUS: "dpu-deploy"}
    last = edge_last.get(selected)
    phases = list(profile.phases)
    if last and name == "build":
        phases = list(ARTIFACT_PHASES[:ARTIFACT_PHASES.index(last) + 1]) + ["report"]
    return tuple(phases)
