from pathlib import Path
import unittest

from tools.farnet_validation.checks import (alarm_check, basebackup_check, frontier_checks,
                                             gate_check, parse_events, teardown_checks)
from tools.farnet_validation.model import Verdict

FIX = Path(__file__).parent / "fixtures"


class CheckTests(unittest.TestCase):
    def fixture(self, name): return (FIX / name).read_text()

    def test_structured_field_order_after_prefix(self):
        events, checks = parse_events("prefix HOMER_EVENT v=1 severity=info component=dma event=done run=r z=2 a=1")
        self.assertFalse(checks); self.assertEqual(events[0].fields, {"z": "2", "a": "1"})

    def test_unknown_event_version_inconclusive(self):
        _, checks = parse_events("HOMER_EVENT v=2 severity=info component=dma event=done run=r")
        self.assertEqual(checks[0].verdict, Verdict.INCONCLUSIVE)

    def test_untyped_session_inconclusive(self):
        _, checks = parse_events("HOMER_EVENT v=1 severity=info component=dma event=done run=r session=3")
        self.assertEqual(checks[0].verdict, Verdict.INCONCLUSIVE)

    def test_truncated_event_is_inconclusive(self):
        _, checks = parse_events(
            "HOMER_EVENT v=1 severity=info component=dma event=done run=r truncated=1")
        self.assertEqual(checks[0].verdict, Verdict.INCONCLUSIVE)

    def test_gate_positive(self):
        result = gate_check(self.fixture("gate_debug.log"), self.fixture("gate_dpu.log"), 5, True)
        self.assertEqual(result.verdict, Verdict.PASS)

    def test_gate_duplicate_anchor_inconclusive(self):
        client = self.fixture("gate_debug.log")
        result = gate_check(client + "\n" + client, self.fixture("gate_dpu.log"), 5, True)
        self.assertEqual(result.verdict, Verdict.INCONCLUSIVE)

    def test_gate_structured_spawn_pair(self):
        dpu = "\n".join((
            "HOMER_EVENT v=1 severity=info component=dpu_spawn event=begin run=r "
            "session_kind=service session=9 handle=2 slot=16 db=5 user=10 bridge_generation=7",
            "HOMER_EVENT v=1 severity=info component=dpu_spawn event=complete run=r "
            "session_kind=service session=9 handle=2 slot=16 launched_pid=22 state_reads=4",
        ))
        self.assertEqual(gate_check(self.fixture("gate_debug.log"), dpu, 5, True).verdict,
                         Verdict.PASS)

    def test_gate_dual_emission_disagreement_is_inconclusive(self):
        structured = (
            "HOMER_EVENT v=1 severity=info component=dpu_spawn event=begin run=r "
            "session_kind=service session=9 handle=2 slot=16 db=5 user=10 bridge_generation=7\n"
            "HOMER_EVENT v=1 severity=info component=dpu_spawn event=complete run=r "
            "session_kind=service session=9 handle=2 slot=16 launched_pid=22 state_reads=4\n")
        legacy = self.fixture("gate_dpu.log")
        self.assertEqual(gate_check(self.fixture("gate_debug.log"), structured + legacy, 5, True).verdict,
                         Verdict.INCONCLUSIVE)

    def test_gate_duplicate_structured_spawn_identity_is_inconclusive(self):
        begin = ("HOMER_EVENT v=1 severity=info component=dpu_spawn event=begin run=r "
                 "session_kind=service session=9 handle=2 slot=16 db=5 user=10 bridge_generation=7\n")
        complete = ("HOMER_EVENT v=1 severity=info component=dpu_spawn event=complete run=r "
                    "session_kind=service session=9 handle=2 slot=16 launched_pid=22 state_reads=4\n")
        self.assertEqual(gate_check(self.fixture("gate_debug.log"), begin * 2 + complete * 2, 5, True).verdict,
                         Verdict.INCONCLUSIVE)

    def test_gate_duplicate_legacy_identity_beside_structured_is_inconclusive(self):
        structured = (
            "HOMER_EVENT v=1 severity=info component=dpu_spawn event=begin run=r "
            "session_kind=service session=1 handle=1 slot=16 db=5 user=10 bridge_generation=7\n"
            "HOMER_EVENT v=1 severity=info component=dpu_spawn event=complete run=r "
            "session_kind=service session=1 handle=1 slot=16 launched_pid=22 state_reads=4\n")
        legacy = self.fixture("gate_dpu.log")
        self.assertEqual(gate_check(self.fixture("gate_debug.log"), structured + legacy * 2, 5, True).verdict,
                         Verdict.INCONCLUSIVE)

    def test_gate_failed_transaction_is_fail(self):
        client = self.fixture("gate_debug.log").replace("failed transactions: 0", "failed transactions: 1")
        self.assertEqual(gate_check(client, self.fixture("gate_dpu.log"), 5).verdict, Verdict.FAIL)

    def test_basebackup_wrap_positive(self):
        result = basebackup_check(self.fixture("basebackup_sender.log"), self.fixture("basebackup_consumer.log"))
        self.assertEqual(result.verdict, Verdict.PASS); self.assertGreater(result.values["full_laps"], 40000)

    def test_basebackup_geometry_mismatch_fails(self):
        consumer = self.fixture("basebackup_consumer.log").replace("requested_slots=4", "requested_slots=3")
        self.assertEqual(basebackup_check(self.fixture("basebackup_sender.log"), consumer).verdict, Verdict.FAIL)

    def test_alarm_vocabulary(self):
        self.assertEqual(alarm_check("DPU DMA engine is in fatal error state").verdict, Verdict.FAIL)

    def test_empty_alarm_role_is_inconclusive(self):
        self.assertEqual(alarm_check("healthy role", "").verdict, Verdict.INCONCLUSIVE)

    def test_basebackup_requires_consumer_wait_status(self):
        consumer = self.fixture("basebackup_consumer.log").replace("CONSUMER_WAIT_RC=0", "NO_WAIT_RC=0")
        self.assertEqual(basebackup_check(self.fixture("basebackup_sender.log"), consumer).verdict,
                         Verdict.INCONCLUSIVE)

    def test_stage2b_ledgers_positive(self):
        checks = teardown_checks(self.fixture("stage2b_ledgers.log"))
        self.assertTrue(checks); self.assertTrue(all(c.verdict == Verdict.PASS for c in checks))

    def test_structured_teardown_ledgers_positive(self):
        text = "\n".join((
            "HOMER_EVENT v=1 severity=info component=service event=head_mirror_accounting run=r "
            "registered=2 released_on_rebind=1 retired_on_clear=1 retired_at_exit=0 "
            "retired_on_open_failure=0 live=0",
            "HOMER_EVENT v=1 severity=info component=peer_transport event=control_retirement run=r "
            "retiring_entered=0 released_normally=0 reset_discarded=0 live=0 "
            "abandoned_partial_publish=0 abandoned_stale_async=0",
            "HOMER_EVENT v=1 severity=info component=service event=stage2b_teardown run=r "
            "reset_owned=0x0000000000000000 reset_runnable=0x0000000000000000 "
            "completion_transport_blocked=0x0000000000000000 active_shards=0 peak_shards=1 "
            "shard_alloc_failures=0",
            "HOMER_EVENT v=1 severity=info component=dpu_dma event=teardown_complete run=r "
            "outstanding=0 progress_rounds=0 free_slots=3072 total_slots=3072 fatal=false",
            "HOMER_EVENT v=1 severity=info component=peer_transport event=send_frontier run=r "
            "peer=10.10.1.200 generation=2 posted=12 retired=12 flushed=0 signalled_post=7 "
            "signalled_retired=7 force_checkpoint=0",
        ))
        self.assertTrue(all(c.verdict == Verdict.PASS for c in teardown_checks(text)))

    def test_frontier_delta_fails(self):
        text = "peer send frontier host=x generation=1 posted=4 retired=2 flushed=1"
        self.assertEqual(frontier_checks(text)[0].verdict, Verdict.FAIL)

    def test_missing_ledger_inconclusive(self):
        self.assertEqual(teardown_checks("nothing")[0].verdict, Verdict.INCONCLUSIVE)


if __name__ == "__main__": unittest.main()
