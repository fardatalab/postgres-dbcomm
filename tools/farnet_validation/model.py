"""Typed outcomes shared by the phase engine and evidence parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    AWAITING_AGENT = "AWAITING_AGENT"
    INVALID = "INVALID"

    @property
    def exit_code(self) -> int:
        return {self.PASS: 0, self.FAIL: 1, self.INCONCLUSIVE: 2,
                self.AWAITING_AGENT: 2, self.INVALID: 3}[self]


class FailureKind(str, Enum):
    CORRECTNESS = "correctness"
    BUILD = "build"
    WORKLOAD = "workload"
    TIMEOUT = "timeout"
    IDENTITY = "identity"
    ENVIRONMENT = "environment"
    EVIDENCE = "evidence"
    UNKNOWN_SCHEMA = "unknown-schema"
    INVOCATION = "invocation"


@dataclass(frozen=True)
class CheckResult:
    name: str
    verdict: Verdict
    detail: str
    failure_kind: FailureKind | None = None
    values: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "verdict": self.verdict.value,
                "detail": self.detail,
                "failure_kind": self.failure_kind.value if self.failure_kind else None,
                "values": dict(self.values)}


def combine(results: list[CheckResult]) -> Verdict:
    """Return the most conservative verdict; an unknown can never become PASS."""
    order = {Verdict.PASS: 0, Verdict.FAIL: 1, Verdict.INCONCLUSIVE: 2,
             Verdict.AWAITING_AGENT: 3, Verdict.INVALID: 4}
    return max((r.verdict for r in results), key=order.get, default=Verdict.INCONCLUSIVE)
