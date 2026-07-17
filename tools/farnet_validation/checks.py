"""Structured-event and bounded legacy adapters for acceptance evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .model import CheckResult, FailureKind, Verdict


EVENT = re.compile(r"HOMER_EVENT\s+([^\r\n]+)")
TOKEN = re.compile(r"^[^\s=]+=[^\s]+$")
ALARM = re.compile(
    r"ALARM|semantic validation failed|fatal error state|PE drain failed|pool exhausted|peer-open failed",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HomerEvent:
    version: int
    severity: str
    component: str
    event: str
    run: str
    fields: dict[str, str]
    raw: str


def parse_events(text: str) -> tuple[list[HomerEvent], list[CheckResult]]:
    events: list[HomerEvent] = []
    checks: list[CheckResult] = []
    for match in EVENT.finditer(text):
        raw = match.group(0)
        parts = match.group(1).split()
        if len(parts) < 5 or any(not TOKEN.match(part) for part in parts):
            checks.append(CheckResult("event-schema", Verdict.INCONCLUSIVE,
                                      f"malformed event: {raw}", FailureKind.UNKNOWN_SCHEMA))
            continue
        pairs = [part.split("=", 1) for part in parts]
        required = ["v", "severity", "component", "event", "run"]
        if [key for key, _ in pairs[:5]] != required:
            checks.append(CheckResult("event-schema", Verdict.INCONCLUSIVE,
                                      f"required prefix out of order: {raw}", FailureKind.UNKNOWN_SCHEMA))
            continue
        fields = dict(pairs)
        if fields["v"] != "1" or fields["severity"] not in {"info", "warn", "alarm"}:
            checks.append(CheckResult("event-schema", Verdict.INCONCLUSIVE,
                                      f"unknown version/severity: {raw}", FailureKind.UNKNOWN_SCHEMA))
            continue
        if len(fields) != len(pairs):
            checks.append(CheckResult("event-schema", Verdict.INCONCLUSIVE,
                                      f"duplicate fields: {raw}", FailureKind.UNKNOWN_SCHEMA))
            continue
        if "session" in fields and "session_kind" not in fields:
            checks.append(CheckResult("event-schema", Verdict.INCONCLUSIVE,
                                      f"untyped session: {raw}", FailureKind.UNKNOWN_SCHEMA))
            continue
        if fields.get("truncated") == "1":
            checks.append(CheckResult("event-schema", Verdict.INCONCLUSIVE,
                                      f"truncated event cannot prove acceptance: {raw}",
                                      FailureKind.UNKNOWN_SCHEMA))
            continue
        events.append(HomerEvent(1, fields.pop("severity"), fields.pop("component"),
                                 fields.pop("event"), fields.pop("run"),
                                 {k: v for k, v in fields.items() if k != "v"}, raw))
    return events, checks


def alarm_check(*texts: str) -> CheckResult:
    if not texts or any(not text.strip() for text in texts):
        return CheckResult("alarms", Verdict.INCONCLUSIVE,
                           "one or more required role logs are absent or empty",
                           FailureKind.EVIDENCE,
                           {"roles": len(texts),
                            "empty_roles": sum(not text.strip() for text in texts)})
    structured = []
    schema_checks = []
    legacy = []
    for text in texts:
        events, checks = parse_events(text)
        schema_checks.extend(checks)
        structured.extend(event for event in events if event.severity == "alarm")
        legacy.extend(ALARM.findall(text))
    if schema_checks:
        return schema_checks[0]
    if structured or legacy:
        return CheckResult("alarms", Verdict.FAIL, "alarm/fatal evidence present",
                           FailureKind.CORRECTNESS,
                           {"structured": len(structured), "legacy_matches": len(legacy)})
    return CheckResult("alarms", Verdict.PASS, "no structured or legacy alarm/fatal anchor")


def gate_check(client: str, backend_dpu: str, expected_transactions: int,
               require_debug_values: bool = False) -> CheckResult:
    transport = len(re.findall(r"^transport: homer-dpu\S* \(implies dpu result relay\)$", client, re.M))
    processed = re.findall(r"number of transactions actually processed: (\d+)/(\d+)", client)
    failed = re.findall(r"number of failed transactions: (\d+)", client)
    events, schema_checks = parse_events(backend_dpu)
    if schema_checks:
        return schema_checks[0]
    structured_begins = [event for event in events
                         if event.component == "dpu_spawn" and event.event == "begin"]
    structured_completed = [event for event in events
                            if event.component == "dpu_spawn" and event.event == "complete"]
    legacy_begins = re.findall(r"DPU backend spawn begin handle=(\d+) slot=(\d+) session=(\d+)", backend_dpu)
    legacy_completed = re.findall(
        r"DPU backend spawn COMPLETED handle=(\d+) slot=(\d+) session=(\d+) launched_pid=\d+", backend_dpu)
    if structured_begins or structured_completed:
        begin_keys = {(event.fields.get("session"), event.fields.get("handle"), event.fields.get("slot"))
                      for event in structured_begins}
        complete_keys = {(event.fields.get("session"), event.fields.get("handle"), event.fields.get("slot"))
                         for event in structured_completed}
        begins, completed = len(structured_begins), len(structured_completed)
        spawn_correlated = (begin_keys == complete_keys and len(begin_keys) == begins and
                            len(complete_keys) == completed and
                            None not in {item for key in begin_keys | complete_keys for item in key})
        if legacy_begins or legacy_completed:
            legacy_begin_keys = {(session, handle, slot) for handle, slot, session in legacy_begins}
            legacy_complete_keys = {(session, handle, slot) for handle, slot, session in legacy_completed}
            spawn_correlated = (spawn_correlated and begin_keys == legacy_begin_keys and
                                complete_keys == legacy_complete_keys and
                                len(legacy_begin_keys) == len(legacy_begins) and
                                len(legacy_complete_keys) == len(legacy_completed))
    else:
        begins, completed = len(legacy_begins), len(legacy_completed)
        spawn_correlated = (sorted(legacy_begins) == sorted(legacy_completed) and
                            len(set(legacy_begins)) == begins and len(set(legacy_completed)) == completed)
    eos = re.findall(r"DPU result relay for sql_execute reached EOS: rows=1 abalance=(-?\d+)", client)
    if transport != 1 or len(processed) != 1 or len(failed) != 1:
        return CheckResult("gate", Verdict.INCONCLUSIVE, "missing or duplicated client path anchors",
                           FailureKind.EVIDENCE,
                           {"transport": transport, "processed_anchors": len(processed), "failed_anchors": len(failed)})
    done, requested = map(int, processed[0])
    if requested != expected_transactions or done != requested or int(failed[0]) != 0:
        return CheckResult("gate", Verdict.FAIL, "gate transaction accounting disproves success",
                           FailureKind.CORRECTNESS,
                           {"processed": done, "requested": requested, "failed": int(failed[0])})
    if begins < 1 or completed < 1 or begins != completed or not spawn_correlated:
        return CheckResult("gate", Verdict.INCONCLUSIVE, "backend DPU spawn proof missing or contradictory",
                           FailureKind.EVIDENCE, {"spawn_begin": begins, "spawn_completed": completed,
                                                  "correlated": spawn_correlated})
    if require_debug_values and (len(eos) != expected_transactions or len(set(eos)) != expected_transactions):
        return CheckResult("gate", Verdict.INCONCLUSIVE, "decoded debug values are absent or not distinct",
                           FailureKind.EVIDENCE, {"eos": len(eos), "distinct": len(set(eos))})
    return CheckResult("gate", Verdict.PASS, "DPU command/result path and backend spawn proved",
                       values={"transactions": done, "spawn_pairs": begins, "decoded_values": len(eos)})


def basebackup_check(sender: str, consumer: str, slots: int = 4,
                     ring_bytes: int = 524288) -> CheckResult:
    sender_done = len(re.findall(r"base backup completed", sender))
    matches = re.findall(
        r"consuming basebackup stream node=(\d+) db=(\d+) user=(\d+) requested_slots=(\d+) bytes=(\d+) tag=(\S+).*?"
        r"stream complete, delivered_bytes=(\d+)", consumer, re.S)
    consumer_rc = re.findall(r"(?m)^CONSUMER_WAIT_RC=(\d+)\b", consumer)
    if sender_done != 1 or len(matches) != 1 or len(consumer_rc) != 1:
        return CheckResult("basebackup", Verdict.INCONCLUSIVE,
                           "missing or duplicated sender/consumer terminal anchors", FailureKind.EVIDENCE,
                           {"sender_completed": sender_done, "consumer_pairs": len(matches),
                            "consumer_statuses": len(consumer_rc)})
    if int(consumer_rc[0]) != 0:
        return CheckResult("basebackup", Verdict.FAIL, "basebackup consumer exited nonzero",
                           FailureKind.WORKLOAD, {"consumer_rc": int(consumer_rc[0])})
    node, _db, _user, got_slots, got_bytes, _tag, delivered = matches[0]
    delivered_i = int(delivered)
    if int(node) != 2 or int(got_slots) != slots or int(got_bytes) != ring_bytes:
        return CheckResult("basebackup", Verdict.FAIL, "basebackup role or geometry mismatch",
                           FailureKind.CORRECTNESS,
                           {"node": node, "slots": got_slots, "bytes": got_bytes})
    laps, remainder = divmod(delivered_i, ring_bytes)
    if delivered_i <= 0 or laps < 2:
        return CheckResult("basebackup", Verdict.FAIL, "workload did not prove a byte-ring wrap",
                           FailureKind.CORRECTNESS,
                           {"delivered_bytes": delivered_i, "full_laps": laps})
    return CheckResult("basebackup", Verdict.PASS, "both roles completed with wrap proof",
                       values={"delivered_bytes": delivered_i, "full_laps": laps,
                               "remainder": remainder, "slots": slots, "ring_bytes": ring_bytes})


def frontier_checks(text: str) -> list[CheckResult]:
    events, schema_checks = parse_events(text)
    if schema_checks:
        return [schema_checks[0]]
    structured = [event for event in events
                  if event.component == "peer_transport" and event.event == "send_frontier"]
    structured_rows = [(event.fields.get("generation", ""), event.fields.get("posted", ""),
                        event.fields.get("retired", ""), event.fields.get("flushed", ""))
                       for event in structured]
    legacy_rows = re.findall(
        r"peer send frontier .*?generation=(\d+) posted=(\d+) retired=(\d+) flushed=(\d+)", text)
    if structured_rows and legacy_rows and sorted(structured_rows) != sorted(legacy_rows):
        return [CheckResult("frontiers", Verdict.INCONCLUSIVE,
                            "structured and legacy frontier records disagree", FailureKind.EVIDENCE)]
    rows = structured_rows or legacy_rows
    if not rows:
        return [CheckResult("frontiers", Verdict.INCONCLUSIVE, "missing frontier ledger",
                            FailureKind.EVIDENCE)]
    results = []
    for generation, posted, retired, flushed in rows:
        try:
            values = tuple(map(int, (posted, retired, flushed)))
        except ValueError:
            return [CheckResult("frontiers", Verdict.INCONCLUSIVE, "frontier fields are not integers",
                                FailureKind.UNKNOWN_SCHEMA)]
        verdict = Verdict.PASS if values[0] == values[1] + values[2] else Verdict.FAIL
        results.append(CheckResult(f"frontier-generation-{generation}", verdict,
                                   "posted equals retired plus flushed" if verdict == Verdict.PASS else
                                   "unexplained posted frontier delta",
                                   None if verdict == Verdict.PASS else FailureKind.CORRECTNESS,
                                   {"posted": values[0], "retired": values[1], "flushed": values[2]}))
    return results


def teardown_checks(text: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    events, schema_checks = parse_events(text)
    if schema_checks:
        return [schema_checks[0]]
    stage2b_events = [event for event in events
                      if event.component == "service" and event.event == "stage2b_teardown"]
    mirror_events = [event for event in events
                     if event.component == "service" and event.event == "head_mirror_accounting"]
    control_events = [event for event in events
                      if event.component == "peer_transport" and event.event == "control_retirement"]
    dma_events = [event for event in events
                  if event.component == "dpu_dma" and event.event == "teardown_complete"]
    structured_ledger = [tuple(event.fields.get(key, "") for key in
                               ("reset_owned", "reset_runnable", "completion_transport_blocked", "active_shards",
                                "peak_shards", "shard_alloc_failures")) for event in stage2b_events]
    legacy_ledger = re.findall(
        r"Stage-2b teardown ledger reset_owned=(\S+) reset_runnable=(\S+) completion_transport_blocked=(\S+) "
        r"active_shards=(\d+) peak_shards=(\d+) shard_alloc_failures=(\d+)", text)
    structured_mirror = [tuple(event.fields.get(key, "") for key in
                               ("registered", "released_on_rebind", "retired_on_clear", "retired_at_exit",
                                "retired_on_open_failure", "live")) for event in mirror_events]
    legacy_mirror = re.findall(
        r"head-mirror MR accounting: registered=(\d+) released_on_rebind=(\d+) retired_on_clear=(\d+) "
        r"retired_at_exit=(\d+) retired_on_open_failure=(\d+) live=(\d+)", text)
    structured_control = [tuple(event.fields.get(key, "") for key in
                                ("live", "abandoned_partial_publish", "abandoned_stale_async"))
                          for event in control_events]
    legacy_control = re.findall(
        r"peer control retirement accounting: .*?live=(\d+) "
        r"abandoned_partial_publish=(\d+) abandoned_stale_async=(\d+)", text)
    structured_dma = [tuple(event.fields.get(key, "") for key in
                            ("outstanding", "free_slots", "total_slots", "fatal"))
                      for event in dma_events]
    legacy_dma = re.findall(
        r"teardown drain COMPLETE: entered with (\d+) outstanding task\(s\).*?"
        r"free_slots=(\d+)/(\d+) fatal=(\w+)", text)
    for name, structured_rows, legacy_rows in (
            ("stage2b", structured_ledger, legacy_ledger), ("mirror", structured_mirror, legacy_mirror),
            ("control", structured_control, legacy_control), ("dma", structured_dma, legacy_dma)):
        if structured_rows and legacy_rows and sorted(structured_rows) != sorted(legacy_rows):
            return [CheckResult("teardown-ledgers", Verdict.INCONCLUSIVE,
                                f"structured and legacy {name} records disagree", FailureKind.EVIDENCE)]
    ledger = structured_ledger or legacy_ledger
    mirror = structured_mirror or legacy_mirror
    control = structured_control or legacy_control
    dma = structured_dma or legacy_dma
    if not ledger or not mirror or not control or not dma:
        return [CheckResult("teardown-ledgers", Verdict.INCONCLUSIVE,
                            "one or more teardown ledger families are missing", FailureKind.EVIDENCE,
                            {"stage2b": len(ledger), "mirror": len(mirror),
                             "control": len(control), "dma": len(dma)})]
    try:
        bad_ledger = [row for row in ledger if row[0:3] != ("0x0000000000000000",) * 3 or
                      int(row[3]) != 0 or int(row[4]) <= 0 or int(row[5]) != 0]
        bad_mirror = [row for row in mirror if int(row[5]) != 0 or int(row[0]) != sum(map(int, row[1:5]))]
        bad_control = [row for row in control if any(map(int, row))]
        bad_dma = [row for row in dma if int(row[1]) != int(row[2]) or row[3] != "false"]
    except ValueError:
        return [CheckResult("teardown-ledgers", Verdict.INCONCLUSIVE,
                            "teardown fields are not valid integers", FailureKind.UNKNOWN_SCHEMA)]
    verdict = Verdict.PASS if not (bad_ledger or bad_mirror or bad_control or bad_dma) else Verdict.FAIL
    checks.append(CheckResult("teardown-ledgers", verdict,
                              "reset/shard/mirror/control/DMA ledgers balanced" if verdict == Verdict.PASS else
                              "nonzero, vacuous, or contradictory teardown accounting",
                              None if verdict == Verdict.PASS else FailureKind.CORRECTNESS,
                              {"stage2b": len(ledger), "mirror": len(mirror),
                               "control": len(control), "dma": len(dma)}))
    checks.extend(frontier_checks(text))
    return checks
