# Python v3.12 / Pydantic v2.14.1 / limits v5.8.0
# Real admission, verifiers, projector and native quota; only external reads are controlled.
import threading  # Coordinate actual worker lifetimes without timing-dependent sleeps.
import unittest  # Use the repository's offline runner.
import warnings  # Assert malformed native output never prints stored values as diagnostics.
from concurrent.futures import CancelledError, TimeoutError  # Native caller wait outcomes.
from dataclasses import FrozenInstanceError  # Enforce immutable local result snapshots.
from unittest.mock import patch  # Inject explicit boundary failures while keeping native checks.
from uuid import UUID  # Construct independent historical offer identities.

from limits.storage import MemoryStorage  # Keep actual moving-window state and effects.
from pydantic import ValidationError  # Native rejection remains visible before dispatch.
from pydantic_core import PydanticSerializationError  # Native serializer can reject corrupt instances.

import shopunow.admission as admission  # Missing production entry gives an assertion RED.
from shopunow.api import project_history_response  # Exercise the actual approved projector.
from shopunow.allowances import Allowance, RollingAllowances
from shopunow.schemas import HistorySelectors
from tests.test_admission import InlineExecutor, ObservedLock, ObservedThreadPool, SubmitThenRaise
from tests.test_question_admission import SimulatedReaders  # Existing native flat-read fixtures.
from tests.test_records import (
    CONVERSATION_ID, HEADER, USER_ID, event_result, header_result, transcript_pair,
    transcript_marker, transcript_offer,
)

READ_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"  # Reference for this read, not a saved exchange.


class HistoryAdmissionTests(unittest.TestCase):
    """Catch bypassed verification, early charging, stale output and premature capacity release."""

    def setUp(self):
        self.slots = admission.ExecutionSlots()
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.allowances = RollingAllowances(self.storage, threading.Lock())
        self.readers = SimulatedReaders()
        self.now = 100.0
        self.executor = ObservedThreadPool(max_workers=12)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)

    def submit(self, *, selectors=None, executor=None, readers=None, projector=None, **kwargs):
        self.assertTrue(callable(getattr(admission, "submit_history_admission", None)),
                        "The synchronous history admission entry is missing")
        return admission.submit_history_admission(
            {"request_id": READ_ID, "user_id": USER_ID, "conversation_id": CONVERSATION_ID}
            if selectors is None else selectors,
            slots=self.slots, allowances=self.allowances, executor=executor or InlineExecutor(),
            readers=readers or self.readers, projector=projector or project_history_response,
            clock=kwargs.pop("clock", lambda: self.now), **kwargs,
        )

    def result(self, **kwargs):
        return self.submit(**kwargs).result(3)

    def assert_result(self, result, status, charged=False, decision=None):
        self.assertEqual(result.status, status)
        self.assertIs(result.charged, charged)
        self.assertEqual(result.quota_decision, decision)
        self.assertEqual(result.response is not None, status == "ready")
        self.assertNotIn("Stored answer", repr(result))
        self.assertNotIn("Help with", repr(result))

    def remaining(self):
        return sum(self.allowances.try_commit(Allowance.HISTORY) for _ in range(31))

    def block(self, readers, kind):
        started, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def blocked():
            started.set()
            if not finish.wait(3):
                raise AssertionError("The test did not release its controlled read.")

        readers.hooks[kind] = blocked
        return started, finish

    def with_offers(self, readers=None):
        readers = readers or self.readers
        readers.event_value = event_result(*transcript_pair(with_offer=True),
                                          *transcript_pair(2, 3, with_offer=True))
        readers.offers = {str(UUID(int=i + 1000)): transcript_offer(i) for i in (1, 2)}
        return readers

    def test_empty_owned_history_is_ready_and_charges_history_only(self):
        # Bypassing the final hit or charging PROFILE_READ would break these native effects.
        handle = self.submit()
        result = handle.result(0)
        self.assert_result(result, "ready", True, "admitted")
        self.assertEqual(result.response.model_dump(), {
            "request_id": READ_ID, "user_id": USER_ID, "conversation_id": CONVERSATION_ID,
            "messages": (), "notices": (),
        })
        self.assertEqual(self.readers.calls, [
            ("profile", USER_ID, 8.0, None), ("header", CONVERSATION_ID, 8.0, None),
            ("event", CONVERSATION_ID, 8.0, 51),
        ])
        self.assertEqual(self.remaining(), 29)
        for category, count in [(Allowance.QUESTION, 10), (Allowance.PROFILE_READ, 30),
                                (Allowance.WRITE, 10)]:
            self.assertTrue(all(self.allowances.try_commit(category) for _ in range(count)))
        with self.assertRaises(FrozenInstanceError):
            result.status = "stopped"

    def test_profile_failure_precedes_header_without_charge(self):
        for raw, expected in [(event_result(), "profile_unavailable"), ({}, "unresolved")]:
            readers = SimulatedReaders()
            readers.profile_value = raw
            self.assert_result(self.result(readers=readers), expected)
            self.assertEqual([c[0] for c in readers.calls], ["profile"])
        self.assertEqual(self.remaining(), 30)

    def test_absent_and_foreign_headers_share_safe_failure(self):
        foreign = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
        foreign["metadatas"][0]["owner_user_id"] = "syn-cust-02"
        results = []
        for raw in [event_result(), foreign]:
            readers = SimulatedReaders()
            readers.header_value = raw
            results.append(self.result(readers=readers))
            self.assertEqual([c[0] for c in readers.calls], ["profile", "header"])
        self.assertEqual(results[0], results[1])
        self.assert_result(results[0], "ownership_failure")
        self.assertEqual(self.remaining(), 30)

    def test_corrupt_header_is_unresolved_without_event_read(self):
        self.readers.header_value["documents"] = ["{}"]
        self.assert_result(self.result(), "conversation_unresolved")
        self.assertEqual(len(self.readers.calls), 2)

    def test_all_events_pass_before_any_offer_read(self):
        self.readers.event_value = event_result(*transcript_pair(with_offer=True),
                                              transcript_pair(2, 3)[0])
        self.assert_result(self.result(), "conversation_unresolved")
        self.assertEqual([c[0] for c in self.readers.calls], ["profile", "header", "event"])
        self.assertEqual(self.remaining(), 30)

    def test_exact_offers_and_actual_projection_precede_hit(self):
        self.with_offers()
        projected = []
        real_hit = self.allowances._try_commit_locked

        def project(transcript, *, selectors):
            response = project_history_response(transcript, selectors=selectors)
            projected.append(response)
            return response

        def hit(category):
            self.assertEqual(category, Allowance.HISTORY)
            self.assertEqual(len(projected[0].messages), 4)
            self.assertEqual([c[1] for c in self.readers.calls if c[0] == "offer"], [
                "00000000-0000-0000-0000-0000000003e9", "00000000-0000-0000-0000-0000000003ea",
            ])
            return real_hit(category)

        with patch.object(self.allowances, "_try_commit_locked", side_effect=hit):
            result = self.result(projector=project)
        self.assert_result(result, "ready", True, "admitted")
        self.assertEqual(result.response.messages[-1].text, "Stored answer 2")

    def test_wrong_offer_request_is_unresolved_without_partial_response(self):
        self.with_offers()
        self.readers.offers[str(UUID(int=1002))] = transcript_offer(1)
        self.assert_result(self.result(), "conversation_unresolved")
        self.assertEqual(self.remaining(), 30)

    def test_full_fifty_events_readable_and_overflow_never_truncated(self):
        for count, expected in [(50, "ready"), (51, "conversation_unresolved")]:
            readers = SimulatedReaders()
            readers.event_value = event_result(*(transcript_marker(i) for i in range(1, count + 1)))
            readers.offers = {str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain")
                              for i in range(1, count + 1)}
            result = self.result(readers=readers)
            self.assert_result(result, expected, count == 50, "admitted" if count == 50 else None)
            self.assertEqual(sum(c[0] == "offer" for c in readers.calls), 50 if count == 50 else 0)
            if count == 50:
                self.assertEqual(len(result.response.notices), 50)
        self.assertEqual(self.remaining(), 29)

    def test_quota_exhaustion_follows_complete_projection(self):
        for _ in range(30):
            self.assertTrue(self.allowances.try_commit(Allowance.HISTORY))
        self.readers.event_value = event_result(transcript_pair()[0])
        self.assert_result(self.result(), "conversation_unresolved")
        self.readers.event_value = event_result()
        projected = threading.Event()

        def project(transcript, *, selectors):
            projected.set()
            return project_history_response(transcript, selectors=selectors)

        self.assert_result(self.result(projector=project), "quota_exhausted", False, "rejected")
        self.assertTrue(projected.is_set())

    def test_native_selectors_and_cap_revalidate_before_clock_or_reads(self):
        invalid = HistorySelectors(request_id=READ_ID, user_id=USER_ID, conversation_id=CONVERSATION_ID)
        object.__setattr__(invalid, "user_id", "../invalid")
        for selectors in [invalid, {"request_id": READ_ID, "user_id": USER_ID,
                                    "conversation_id": CONVERSATION_ID, "projector": "chosen"}]:
            with self.subTest(selectors=selectors), self.assertRaises(ValidationError):
                self.submit(selectors=selectors, clock=lambda: self.fail("Clock before validation"))
        for cap in [True, 0, -1, "50"]:
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                self.submit(cap=cap, clock=lambda: self.fail("Clock before cap validation"))
        self.assertEqual(self.readers.calls, [])

    def test_corrupt_native_selector_does_not_warn_with_input_content(self):
        # Native serialization's default warning can otherwise expose forcibly mutated input.
        selectors = HistorySelectors(request_id=READ_ID, user_id=USER_ID,
                                     conversation_id=CONVERSATION_ID)
        object.__setattr__(selectors, "user_id", {"PRIVATE": "malformed selector"})
        with warnings.catch_warnings(record=True) as emitted:
            warnings.simplefilter("always")
            with self.assertRaises((ValidationError, PydanticSerializationError)):
                self.submit(selectors=selectors, clock=lambda: self.fail("Clock before validation"))
        self.assertEqual(emitted, [], "Invalid selectors must not print input values")
        self.assertEqual(self.readers.calls, [])

    def test_selector_snapshot_keeps_empty_and_technical_history_same_operation(self):
        for technical in (False, True):
            readers = SimulatedReaders()
            if technical:
                readers.event_value = event_result(*transcript_pair(kind="failed_technical"))
            selectors = HistorySelectors(request_id=READ_ID, user_id=USER_ID,
                                         conversation_id=CONVERSATION_ID.replace("-", "").upper())
            started, finish = self.block(readers, "profile")
            handle = self.submit(selectors=selectors, readers=readers, executor=self.executor)
            self.assertTrue(started.wait(2))
            object.__setattr__(selectors, "user_id", "syn-cust-02")
            object.__setattr__(selectors, "request_id", str(UUID(int=9)))
            finish.set()
            result = handle.result(2)
            self.assert_result(result, "ready", True, "admitted")
            self.assertEqual(result.response.user_id, USER_ID)
            self.assertEqual(result.response.request_id, READ_ID)
            self.assertEqual(result.response.conversation_id, CONVERSATION_ID)

    def test_projector_failure_invalid_native_data_or_rebinding_never_charge(self):
        def invalid(transcript, *, selectors):
            response = project_history_response(transcript, selectors=selectors)
            return response.model_copy(update={"messages": ("PRIVATE",)})

        def rebound(transcript, *, selectors):
            return project_history_response(transcript, selectors=selectors).model_copy(
                update={"request_id": str(UUID(int=9))})

        def fail(transcript, *, selectors):
            raise RuntimeError("PRIVATE projection")

        for projector in [invalid, rebound, fail, lambda *args, **kwargs: {}]:
            with warnings.catch_warnings(record=True) as emitted:
                warnings.simplefilter("always")
                self.assert_result(self.result(projector=projector), "unresolved")
            self.assertEqual(emitted, [], "Malformed output must not warn with stored content")
        self.assertEqual(self.remaining(), 30)

    def test_single_deadline_gives_each_read_smaller_remaining_timeout(self):
        self.readers.hooks["profile"] = lambda: setattr(self, "now", 110.0)
        self.readers.hooks["header"] = lambda: setattr(self, "now", 113.0)
        self.assert_result(self.result(), "ready", True, "admitted")
        self.assertEqual([c[2] for c in self.readers.calls], [8.0, 5.0, 2.0])

    def test_late_reads_stop_before_next_read_or_charge(self):
        for kind, count in [("profile", 1), ("header", 2), ("event", 3), ("offer", 4)]:
            self.now = 100.0
            readers = self.with_offers(SimulatedReaders())
            readers.hooks[kind] = lambda: setattr(self, "now", 115.0)
            self.assert_result(self.result(readers=readers), "stopped")
            self.assertEqual(len(readers.calls), count)
        self.assertEqual(self.remaining(), 30)

    def test_late_verification_failure_never_selects_old_failure(self):
        for boundary, field, raw in [
            ("verify_profile", "profile_value", event_result()),
            ("verify_owned_conversation_header", "header_value", event_result()),
            ("verify_transcript_events", "event_value", event_result(transcript_pair()[0])),
            ("verify_complete_transcript", "event_value", event_result()),
        ]:
            self.now = 100.0
            readers = SimulatedReaders()
            setattr(readers, field, raw)
            real = getattr(admission, boundary)

            def late(*args, **kwargs):
                result = real(*args, **kwargs)
                self.now = 115.0
                return result

            with patch.object(admission, boundary, side_effect=late):
                self.assert_result(self.result(readers=readers), "stopped")
        self.assertEqual(self.remaining(), 30)

    def test_projection_finishing_or_raising_at_deadline_has_no_payload_or_hit(self):
        for failure in (False, True):
            self.now = 100.0

            def late(transcript, *, selectors):
                response = project_history_response(transcript, selectors=selectors)
                self.now = 115.0
                if failure:
                    raise RuntimeError("PRIVATE late projection")
                return response

            self.assert_result(self.result(projector=late), "stopped")
        self.assertEqual(self.remaining(), 30)

    def test_read_failure_early_is_technical_late_is_stopped(self):
        for kind in ["profile", "header", "event", "offer"]:
            for late in (False, True):
                self.now = 100.0
                readers = self.with_offers(SimulatedReaders())

                def fail():
                    if late:
                        self.now = 115.0
                    raise RuntimeError("PRIVATE storage payload")

                readers.hooks[kind] = fail
                handle = self.submit(readers=readers)
                self.assert_result(handle.result(0), "stopped" if late else "unresolved")
                self.assertTrue(handle.attempt.stopped)
                self.assertEqual(handle.attempt.checked_state(handle.result(0)).code,
                                 "request_timeout" if late else "technical_error")
        self.assertEqual(self.remaining(), 30)

    def test_late_true_false_and_uncertain_hit_all_suppress_payload(self):
        for outcome, charged, decision in [(True, True, "admitted"), (False, False, "rejected"),
                                           (None, None, "unresolved")]:
            self.now = 100.0
            real_hit = self.allowances._try_commit_locked

            def late(category):
                if outcome is True:
                    real_hit(category)
                self.now = 115.0
                if outcome is None:
                    raise RuntimeError("PRIVATE uncertain hit")
                return outcome

            with patch.object(self.allowances, "_try_commit_locked", side_effect=late):
                handle = self.submit()
            self.assert_result(handle.result(0), "stopped", charged, decision)
            self.assertEqual(handle.attempt.checked_state(handle.result(0)).code, "request_timeout")
        self.assertEqual(self.remaining(), 29)

    def test_unknown_hit_stops_and_never_automatically_retries(self):
        for raw in [None, "yes", RuntimeError("PRIVATE")]:
            def uncertain(category):
                if isinstance(raw, Exception):
                    raise raw
                return raw

            with patch.object(self.allowances, "_try_commit_locked", side_effect=uncertain) as hit:
                handle = self.submit()
                self.assert_result(handle.result(0), "unresolved", None, "unresolved")
                self.assertTrue(handle.attempt.stopped)
                handle.attempt.try_admit(self.allowances)
                self.assertEqual(hit.call_count, 1)
                self.assertEqual(handle.attempt.checked_state(handle.result(0)).code, "technical_error")
        self.assertEqual(self.remaining(), 30)

    def test_deadline_waiting_for_allowance_guard_prevents_hit(self):
        reached = threading.Event()
        native = threading.Lock()
        self.allowances._commit_lock = ObservedLock(native, reached.set)
        with native:
            handle = self.submit(executor=self.executor)
            self.assertTrue(reached.wait(2))
            self.now = 115.0
        self.assert_result(handle.result(2), "stopped", False, "stopped")
        self.assertEqual(self.remaining(), 30)

    def test_withdrawal_winning_guard_while_commit_guard_waits_prevents_hit(self):
        reached = threading.Event()
        native = threading.Lock()
        self.allowances._commit_lock = ObservedLock(native, reached.set)
        with native:
            handle = self.submit(executor=self.executor)
            self.assertTrue(reached.wait(2))
            handle.withdraw()  # This would deadlock if the gate took attempt before allowance.
        self.assert_result(handle.result(2), "stopped", False, "stopped")
        self.assertEqual(handle.attempt.checked_state().code, "request_stopped")
        self.assertEqual(self.remaining(), 30)

    def test_running_hit_keeps_attempt_guard_until_charge_is_recorded(self):
        started, finish, withdrawal_started = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        real_hit = self.allowances._try_commit_locked

        def blocked_hit(category):
            charged = real_hit(category)
            started.set()
            if not finish.wait(3):
                raise AssertionError("Hit not released")
            return charged

        with patch.object(self.allowances, "_try_commit_locked", side_effect=blocked_hit):
            handle = self.submit(executor=self.executor)
            self.assertTrue(started.wait(2))
            handle.attempt._lock = ObservedLock(handle.attempt._lock, withdrawal_started.set)
            withdrawing = self.executor.submit(handle.withdraw)
            self.assertTrue(withdrawal_started.wait(2))
            finish.set()
            withdrawing.result(2)
            result = handle.result(2)
        self.assertIs(result.charged, True)
        self.assertEqual(result.quota_decision, "admitted")
        self.assertTrue(handle.attempt.stopped)
        self.assertEqual(handle.attempt.checked_state(result).code, "request_stopped")
        self.assertEqual(self.remaining(), 29)

    def test_history_ignores_existing_conversation_write_reservation(self):
        reservations = admission.ResourceReservations()
        finish = threading.Event()
        self.addCleanup(finish.set)

        def owner(lease):
            reservations.reserve(lease, ("conversation", CONVERSATION_ID))
            reserved.set()
            finish.wait(3)

        reserved = threading.Event()
        lease = self.slots.submit_with_lease(self.executor, owner)
        self.assertTrue(reserved.wait(2))
        self.assert_result(self.result(), "ready", True, "admitted")
        finish.set()
        lease.result(2)

    def test_wait_timeout_and_running_cancel_retain_capacity_through_read_cleanup(self):
        started, finish_read = threading.Event(), threading.Event()
        cleanup, finish_cleanup = threading.Event(), threading.Event()
        self.addCleanup(finish_cleanup.set)
        self.addCleanup(finish_read.set)

        def read_then_cleanup():
            try:
                started.set()
                if not finish_read.wait(3):
                    raise AssertionError("Read not released")
            finally:
                cleanup.set()
                if not finish_cleanup.wait(3):
                    raise AssertionError("Cleanup not released")

        self.readers.hooks["event"] = read_then_cleanup
        handle = self.submit(executor=self.executor)
        self.assertTrue(started.wait(2))
        with self.assertRaises(TimeoutError):
            handle.result(0)
        self.assertFalse(handle.cancel())
        finish_read.set()
        self.assertTrue(cleanup.wait(2))
        blocker = threading.Event()
        self.addCleanup(blocker.set)
        others = [self.slots.submit(self.executor, blocker.wait, 3) for _ in range(4)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit(readers=SimulatedReaders())
        finish_cleanup.set()
        self.assert_result(handle.result(2), "stopped")
        self.executor.dispatched[0].result(2)
        self.assert_result(self.result(readers=SimulatedReaders()), "ready", True, "admitted")
        blocker.set()
        for lease in others:
            lease.result(2)
        self.assertEqual(self.remaining(), 29)

    def test_blocked_projection_retains_capacity_after_withdrawal(self):
        started, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def project(transcript, *, selectors):
            response = project_history_response(transcript, selectors=selectors)
            started.set()
            if not finish.wait(3):
                raise AssertionError("Projection not released")
            return response

        handle = self.submit(projector=project, executor=self.executor)
        self.assertTrue(started.wait(2))
        handle.withdraw()
        blocker = threading.Event()
        self.addCleanup(blocker.set)
        others = [self.slots.submit(self.executor, blocker.wait, 3) for _ in range(4)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit()
        finish.set()
        self.assert_result(handle.result(2), "stopped")
        blocker.set()
        for lease in others:
            lease.result(2)
        self.assertEqual(self.remaining(), 30)

    def test_confirmed_prestart_cancel_releases_slot_and_prevents_reads(self):
        executor = ObservedThreadPool(max_workers=1)
        self.addCleanup(executor.shutdown, wait=True, cancel_futures=True)
        started, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def occupy():
            started.set()
            finish.wait(3)

        executor.submit(occupy)
        self.assertTrue(started.wait(2))
        handle = self.submit(executor=executor)
        self.assertTrue(handle.cancel())
        with self.assertRaises(CancelledError):
            handle.result(0)
        self.assertEqual(self.readers.calls, [])
        finish.set()
        self.assertEqual(self.remaining(), 30)

    def test_dispatch_failure_after_start_stops_without_late_charge(self):
        started, finish = self.block(self.readers, "event")
        with self.assertRaises(RuntimeError):
            self.submit(executor=SubmitThenRaise(self.executor, started=started))
        blocker = threading.Event()
        self.addCleanup(blocker.set)
        leases = [self.slots.submit(self.executor, blocker.wait, 3) for _ in range(4)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit(readers=SimulatedReaders())
        finish.set()
        self.executor.dispatched[0].result(2)
        self.assertEqual(self.remaining(), 30)
        blocker.set()
        for lease in leases:
            lease.result(2)

    def test_full_slots_reject_before_reads_and_preserve_allowance(self):
        blocker = threading.Event()
        self.addCleanup(blocker.set)
        leases = [self.slots.submit(self.executor, blocker.wait, 3) for _ in range(5)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit()
        self.assertEqual(self.readers.calls, [])
        self.assertEqual(self.remaining(), 30)
        blocker.set()
        for lease in leases:
            lease.result(2)
