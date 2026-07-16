"""Explicit artifact compatibility edges and earliest-mismatch invalidation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .evidence import sha256_file


EDGE_ORDER = (
    "source", "citus-build", "citus-install", "postgres-relink",
    "postgres-install", "peer-prefix", "farnet1-dpu", "farnet0-dpu",
)


@dataclass(frozen=True)
class ReceiptEdge:
    name: str
    input_identity: str
    outputs: dict[str, str]
    recipe: dict

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()


def make_edge(name: str, input_identity: str, outputs: list[Path], recipe: dict) -> ReceiptEdge:
    if name not in EDGE_ORDER:
        raise ValueError(f"unknown receipt edge {name}")
    return ReceiptEdge(name, input_identity, {str(path): sha256_file(path) for path in outputs}, recipe)


def validate_chain(edges: list[dict], expected_source: str | None = None,
                   required_last: str = "farnet0-dpu") -> tuple[bool, str | None, str]:
    """Validate a complete receipt prefix through ``required_last``.

    A receipt prefix is never implicitly complete: the caller names its final
    required compatibility edge, and every edge must contain a recipe plus at
    least one output.  This prevents a source-only receipt from authorizing a
    runtime workload against unspecified installed artifacts.
    """
    if required_last not in EDGE_ORDER:
        return False, required_last, "unknown required receipt edge"
    required_count = EDGE_ORDER.index(required_last) + 1
    if not edges:
        return False, "source", "empty receipt chain"
    if len(edges) > required_count:
        return False, required_last, f"receipt must contain exactly {required_count} edges"
    previous = expected_source
    for index, edge in enumerate(edges):
        expected_name = EDGE_ORDER[index]
        if edge.get("name") != expected_name:
            return False, expected_name, f"expected edge {expected_name}"
        if not isinstance(edge.get("recipe"), dict) or not edge["recipe"]:
            return False, expected_name, "receipt recipe is missing"
        if not isinstance(edge.get("outputs"), dict) or not edge["outputs"]:
            return False, expected_name, "receipt outputs are missing"
        if previous is not None and edge.get("input_identity") != previous:
            return False, expected_name, "input identity does not match predecessor"
        for path, digest in edge.get("outputs", {}).items():
            candidate = Path(path)
            if not candidate.is_file() or sha256_file(candidate) != digest:
                return False, expected_name, f"output mismatch: {path}"
        calculated = hashlib.sha256(json.dumps(
            {key: value for key, value in edge.items() if key != "fingerprint"},
            sort_keys=True).encode()).hexdigest()
        if edge.get("fingerprint") != calculated:
            return False, expected_name, "receipt fingerprint mismatch"
        previous = calculated
    if len(edges) != required_count:
        return False, EDGE_ORDER[len(edges)], f"receipt must contain exactly {required_count} edges"
    return True, None, "complete matching chain"


def load_receipt(path: Path) -> dict:
    return json.loads(path.read_text())
