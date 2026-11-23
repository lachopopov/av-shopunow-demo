# Python v3.12 / Pydantic v2.14.1 / limits v5.8.0
# Simulated native read boundaries exercise the real synchronous admission components.
import json  # Change complete stored JSON documents independently of production parsers.
import threading  # Hold actual worker calls until each lifetime assertion is complete.
import unittest  # Match the project's offline verification runner.
from concurrent.futures import CancelledError, TimeoutError  # Real worker/caller wait outcomes.
from unittest.mock import patch  # Inject faults only at local allowance/recording boundaries.
from uuid import UUID  # Supply independent canonical and alternative conversation selectors.

from limits.storage import MemoryStorage  # Keep the actual approved moving-window backend.
from pydantic import ValidationError  # Native request rejection must precede capacity use.

import shopunow.admission as admission  # Missing feature must fail an assertion during RED.
from shopunow.allowances import Allowance, RollingAllowances
from shopunow.schemas import QuestionRequest, InteractionResponse, ConfirmedAttackOutcome
from tests.test_admission import InlineExecutor, ObservedThreadPool, SubmitThenRaise
from tests.test_records import (
    CONVERSATION_ID, HEADER, USER_ID, REQUEST_ID, event_result, header_result, profile_result,
    transcript_marker, transcript_offer, transcript_pair,
)


class SimulatedReaders:
    """Complete native-get-shaped supplied results; no Chroma client or network exists."""

    def __init__(self):
        self.calls = []  # Observe real coordinator storage-call ordering and arguments.
        self.profile_value = profile_result()
        self.header_value = header_result()
        self.event_value = event_result()
        self.offers = {}
        self.hooks = {}  # Model a slow read, cleanup, failure, withdrawal, or elapsed clock.

    def read(self, kind, selector, timeout, limit=None):
        self.calls.append((kind, selector, timeout, limit))
        if kind in self.hooks:
            self.hooks[kind]()  # Hook runs inside the actual read, including simulated cleanup.
        if kind == "offer":
            return self.offers[selector]
        return getattr(self, f"{kind}_value")

    def profile(self, user_id, *, timeout):
        return self.read("profile", user_id, timeout)

    def header(self, conversation_id, *, timeout):
        return self.read("header", conversation_id, timeout)

    def events(self, conversation_id, *, limit, timeout):
        return self.read("event", conversation_id, timeout, limit)

    def exact_offer(self, offer_id, *, timeout):
        return self.read("offer", offer_id, timeout)


class QuestionAdmissionTests(unittest.TestCase):
    """Catch prerequisite bypass, wrong precedence, premature release, and late quota."""

    def setUp(self):
        from tests.verified_creation import verified_tracking
        self.tracking = verified_tracking()
        self.slots = admission.ExecutionSlots()
        self.reservations = admission.ResourceReservations()
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.allowances = RollingAllowances(self.storage, threading.Lock())
        self.readers = SimulatedReaders()
        self.now = 100.0  # One controlled monotonic clock serves coordinator and attempt.
        self.executor = ObservedThreadPool(max_workers=12)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)

    def submit(self, *, request=None, executor=None, readers=None, **kwargs):
        kwargs.setdefault('tracking', self.tracking)
        self.assertTrue(callable(getattr(admission, "submit_question_admission", None)))
        return admission.submit_question_admission(
            {"user_id": USER_ID, "conversation_id": CONVERSATION_ID, "question": "Question?"}
            if request is None else request,
            slots=self.slots, reservations=self.reservations, allowances=self.allowances,
            executor=executor or InlineExecutor(), readers=readers or self.readers,
            clock=lambda: self.now, **kwargs,
        )

    def result(self, **kwargs):
        return self.submit(**kwargs).result(3)

    def assert_result(self, result, status, charged=False, decision=None):
        self.assertEqual(result.status, status)
        self.assertIs(result.charged, charged)
        self.assertEqual(result.quota_decision, decision)
        self.assertFalse(hasattr(result, "transcript"))  # Control output never returns content.
        self.assertNotIn("Question?", repr(result))

    def remaining_questions(self):
        return sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(11))

    def blocked_read(self, readers, kind):
        started, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)  # Always unblock before executor shutdown, even on RED.

        def block():
            started.set()
            if not finish.wait(3):
                raise AssertionError("The test did not release its simulated read.")

        readers.hooks[kind] = block
        return started, finish

    def test_valid_empty_prefix_is_admitted_once_with_native_read_limits(self):
        handle = self.submit()
        result = handle.result(0)
        self.assert_result(result, "admitted", True, "admitted")
        self.assertEqual(result.admission.admitted_at, 100.0)
        self.assertEqual(result.admission.question_deadline, 190.0)
        self.assertEqual(self.readers.calls, [
            ("profile", "syn-cust-01", 8.0, None),
            ("header", "01234567-89ab-4cde-8fab-0123456789ab", 8.0, None),
            ("event", "01234567-89ab-4cde-8fab-0123456789ab", 8.0, 51),
        ])
        self.assertEqual(self.remaining_questions(), 9)
        self.assertTrue(all(self.allowances.try_commit(Allowance.PROFILE_READ) for _ in range(30)))

    def test_incoming_continuation_runs_inside_owned_admission_lease(self):
        observed = []
        def incoming(*, request, profile, transcript, attempt):
            # A second writer must remain excluded for the complete incoming workflow.
            lease = self.slots.submit_with_lease(InlineExecutor(), lambda owner:
                self.reservations.reserve(owner, ('conversation', CONVERSATION_ID)))
            with self.assertRaises(admission.ReservationConflict):
                lease.result(0)
            self.assertTrue(attempt.authorize_normal_work())
            observed.append((request.question, profile.user_id, len(transcript.exchanges)))
            return None  # Internal continuation has no user-visible answer yet.
        try:
            result = self.result(on_admitted=incoming)
        except TypeError as error:
            self.fail(f'Owned incoming continuation is not supported: {error}')
        self.assert_result(result, 'admitted', True, 'admitted')
        self.assertEqual(observed, [('Question?', USER_ID, 0)])

    def test_confirmed_block_retains_initiating_selectors_through_admission(self):
        def incoming(**state):
            return InteractionResponse(user_id=state['profile'].user_id,
                conversation_id=state['request'].conversation_id, request_id=REQUEST_ID,
                outcome=ConfirmedAttackOutcome(kind='confirmed_attack_block',
                                              text='The request cannot be processed.'))
        result = self.result(on_admitted=incoming)
        self.assertEqual(result.interaction.user_id, USER_ID)
        self.assertEqual(result.interaction.conversation_id, CONVERSATION_ID)
        self.assertEqual(result.interaction.outcome.kind, 'confirmed_attack_block')

    def test_continuation_cannot_return_another_users_terminal(self):
        def incoming(**state):
            return InteractionResponse(user_id='syn-cust-02', conversation_id=CONVERSATION_ID,
                request_id=REQUEST_ID, outcome=ConfirmedAttackOutcome(
                    kind='confirmed_attack_block', text='The request cannot be processed.'))
        result = self.result(on_admitted=incoming)
        self.assertEqual(result.status, 'unresolved')
        self.assertIsNone(result.interaction)

    def test_withdrawal_suppresses_prepared_terminal_without_refunding(self):
        def incoming(**state):
            state['attempt'].withdraw()
            return InteractionResponse(user_id=USER_ID, conversation_id=CONVERSATION_ID,
                request_id=REQUEST_ID, outcome=ConfirmedAttackOutcome(
                    kind='confirmed_attack_block', text='The request cannot be processed.'))
        result = self.result(on_admitted=incoming)
        self.assertEqual(result.status, 'stopped')
        self.assertTrue(result.charged)
        self.assertIsNone(result.interaction)


    def test_pre_admission_failure_never_enters_incoming_continuation(self):
        observed = []
        self.readers.profile_value = event_result()
        try:
            result = self.result(on_admitted=lambda **state: observed.append(state))
        except TypeError as error:
            self.fail(f'Owned incoming continuation is not supported: {error}')
        self.assertEqual(result.status, 'profile_unavailable')
        self.assertEqual(observed, [])

    def test_invalid_request_precedes_even_exhausted_capacity(self):
        started, finish = self.blocked_read(self.readers, "profile")
        handles = [self.submit(executor=self.executor) for _ in range(5)]
        self.assertTrue(started.wait(2))
        with self.assertRaises(ValidationError):
            self.submit(request={"user_id": USER_ID, "conversation_id": CONVERSATION_ID,
                                 "question": "   "})
        finish.set()
        for handle in handles:
            handle.result(2)

    def test_full_slots_reject_without_any_reader_or_quota_effect(self):
        blocker = threading.Event()
        self.addCleanup(blocker.set)
        leases = [self.slots.submit(self.executor, blocker.wait, 3) for _ in range(5)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit()
        self.assertEqual(self.readers.calls, [])
        self.assertEqual(self.remaining_questions(), 10)
        blocker.set()
        for lease in leases:
            lease.result(2)

    def test_profile_is_verified_before_header_and_absence_does_not_charge(self):
        for raw, status in [(event_result(), "profile_unavailable"),
                            ({"ids": ["wrong"], "metadatas": [], "documents": []}, "unresolved"),
                            (profile_result({"complete": True}), "unresolved")]:
            with self.subTest(raw=raw):
                readers = SimulatedReaders()
                readers.profile_value = raw
                self.assert_result(self.result(readers=readers), status)
                self.assertEqual([call[0] for call in readers.calls], ["profile"])
        self.assertEqual(self.remaining_questions(), 10)

    def test_absent_and_valid_foreign_headers_have_identical_safe_results(self):
        foreign = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
        foreign["metadatas"][0]["owner_user_id"] = "syn-cust-02"
        results = []
        for raw in [event_result(), foreign]:
            readers = SimulatedReaders()
            readers.header_value = raw
            results.append(self.result(readers=readers))
            self.assertEqual([call[0] for call in readers.calls], ["profile", "header"])
        self.assertEqual(results[0], results[1])
        self.assert_result(results[0], "ownership_failure")
        self.assertEqual(self.remaining_questions(), 10)

    def test_corrupt_header_is_unresolved_and_no_events_are_read(self):
        self.readers.header_value["documents"] = [json.dumps({**HEADER, "complete": False})]
        self.assert_result(self.result(), "conversation_unresolved")
        self.assertEqual([call[0] for call in self.readers.calls], ["profile", "header"])

    def test_corrupt_later_event_prevents_every_offer_read(self):
        self.readers.event_value = event_result(
            *transcript_pair(with_offer=True), transcript_pair(2, 3)[0],
        )
        self.assert_result(self.result(), "conversation_unresolved")
        self.assertEqual([call[0] for call in self.readers.calls], ["profile", "header", "event"])
        self.assertEqual(self.remaining_questions(), 10)

    def test_all_required_exact_offers_are_read_before_admission(self):
        self.readers.event_value = event_result(*transcript_pair(with_offer=True),
                                              *transcript_pair(2, 3, with_offer=True))
        self.readers.offers = {str(UUID(int=i + 1000)): transcript_offer(i) for i in (1, 2)}
        self.assert_result(self.result(), "admitted", True, "admitted")
        self.assertEqual([call[1] for call in self.readers.calls if call[0] == "offer"], [
            "00000000-0000-0000-0000-0000000003e9", "00000000-0000-0000-0000-0000000003ea",
        ])

    def test_wrong_request_offer_binding_is_unresolved_without_charge(self):
        self.readers.event_value = event_result(*transcript_pair(with_offer=True))
        self.readers.offers = {str(UUID(int=1001)): transcript_offer(2)}
        self.assert_result(self.result(), "conversation_unresolved")
        self.assertEqual(self.remaining_questions(), 10)

    def test_48_events_admit_49_and_50_full_51_unresolved_without_truncation(self):
        for count, expected in [(48, "admitted"), (49, "conversation_full"),
                                (50, "conversation_full"), (51, "conversation_unresolved")]:
            with self.subTest(count=count):
                readers = SimulatedReaders()
                readers.event_value = event_result(*(transcript_marker(i) for i in range(1, count + 1)))
                readers.offers = {
                    str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain")
                    for i in range(1, count + 1)
                }
                self.assert_result(self.result(readers=readers), expected, count == 48,
                                   "admitted" if count == 48 else None)
                self.assertEqual(sum(call[0] == "offer" for call in readers.calls),
                                 count if count <= 50 else 0)
        self.assertEqual(self.remaining_questions(), 9)

    def test_corrupt_full_prefix_offer_is_unresolved_instead_of_full(self):
        self.readers.event_value = event_result(*(transcript_marker(i) for i in range(1, 50)))
        self.readers.offers = {str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain")
                              for i in range(1, 50)}
        self.readers.offers[str(UUID(int=1049))] = event_result()
        self.assert_result(self.result(), "conversation_unresolved")
        self.assertEqual(self.remaining_questions(), 10)

    def test_quota_exhaustion_is_checked_only_after_valid_prefix_and_room(self):
        for _ in range(10):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.readers.event_value = event_result(transcript_pair()[0])
        self.assert_result(self.result(), "conversation_unresolved")
        self.readers.event_value = event_result()
        self.assert_result(self.result(), "quota_exhausted", False, "rejected")
        self.assertEqual([call[0] for call in self.readers.calls],
                         ["profile", "header", "event"] * 2)

    def test_read_timeout_is_smaller_of_eight_and_remaining_fifteen(self):
        self.readers.hooks["profile"] = lambda: setattr(self, "now", 110.0)
        self.readers.hooks["header"] = lambda: setattr(self, "now", 113.0)
        self.assert_result(self.result(), "admitted", True, "admitted")
        self.assertEqual([call[2] for call in self.readers.calls], [8.0, 5.0, 2.0])

    def test_late_result_at_any_read_stops_downstream_reads_and_quota(self):
        for kind, expected_calls in [("profile", 1), ("header", 2), ("event", 3), ("offer", 4)]:
            with self.subTest(kind=kind):
                self.now = 100.0
                readers = SimulatedReaders()
                readers.event_value = event_result(*transcript_pair(with_offer=True),
                                                  *transcript_pair(2, 3, with_offer=True))
                readers.offers = {str(UUID(int=i + 1000)): transcript_offer(i) for i in (1, 2)}
                readers.hooks[kind] = lambda: setattr(self, "now", 115.0)
                self.assert_result(self.result(readers=readers), "stopped")
                self.assertEqual(len(readers.calls), expected_calls)
        self.assertEqual(self.remaining_questions(), 10)

    def test_invalid_validation_finishing_after_deadline_selects_stopped(self):
        # Omitting a post-validation permission check would surface a late failure.
        for boundary, field, raw, calls in [
            ("verify_profile", "profile_value", event_result(), 1),
            ("verify_owned_conversation_header", "header_value", event_result(), 2),
            ("verify_transcript_events", "event_value", event_result(transcript_pair()[0]), 3),
        ]:
            with self.subTest(boundary=boundary):
                self.now = 100.0
                readers = SimulatedReaders()
                setattr(readers, field, raw)
                real_verifier = getattr(admission, boundary)

                def finish_late(*args, **kwargs):
                    checked = real_verifier(*args, **kwargs)  # Keep actual native validation.
                    self.now = 115.0
                    return checked

                with patch.object(admission, boundary, side_effect=finish_late):
                    self.assert_result(self.result(readers=readers), "stopped")
                self.assertEqual(len(readers.calls), calls)
        self.assertEqual(self.remaining_questions(), 10)

    def test_read_failures_return_content_free_unresolved_without_charge(self):
        for kind in ("profile", "header", "event", "offer"):
            with self.subTest(kind=kind):
                readers = SimulatedReaders()
                readers.event_value = event_result(*transcript_pair(with_offer=True))
                readers.offers = {str(UUID(int=1001)): transcript_offer()}

                def fail():
                    raise RuntimeError("PRIVATE storage response and customer content")

                readers.hooks[kind] = fail
                result = self.result(readers=readers)
                self.assert_result(result, "unresolved")
                self.assertNotIn("PRIVATE", repr(result))
        self.assertEqual(self.remaining_questions(), 10)

    def test_read_exception_after_deadline_selects_stopped(self):
        # A storage exception cannot turn a logically stopped call into a new visible failure.
        for kind in ("profile", "header", "event", "offer"):
            with self.subTest(kind=kind):
                self.now = 100.0
                readers = SimulatedReaders()
                readers.event_value = event_result(*transcript_pair(with_offer=True))

                def late_failure():
                    self.now = 115.0
                    raise RuntimeError("PRIVATE late storage failure")

                readers.hooks[kind] = late_failure
                self.assert_result(self.result(readers=readers), "stopped")
        self.assertEqual(self.remaining_questions(), 10)

    def test_unexpected_early_failure_stops_attempt_without_losing_charge_state(self):
        def fail():
            raise RuntimeError("PRIVATE read failure")

        self.readers.hooks["profile"] = fail
        early = self.submit()
        self.assert_result(early.result(0), "unresolved")
        self.assertTrue(early.attempt.stopped)
        with patch.object(admission.QuestionAttempt, "authorize_normal_work", side_effect=RuntimeError("PRIVATE")):
            later = self.submit(readers=SimulatedReaders())
        self.assert_result(later.result(0), "unresolved", True, "admitted")
        self.assertTrue(later.attempt.stopped)
        self.assertEqual(self.remaining_questions(), 9)

    def test_unknown_hit_and_expired_post_hit_never_report_admitted(self):
        with patch.object(self.allowances, "_try_commit_locked", side_effect=RuntimeError("PRIVATE")):
            self.assert_result(self.result(), "unresolved", None, "unresolved")
        real_hit = self.allowances._try_commit_locked

        def late_hit(category):
            charged = real_hit(category)
            self.now = 191.0
            return charged

        with patch.object(self.allowances, "_try_commit_locked", side_effect=late_hit):
            self.assert_result(self.result(), "stopped", True, "admitted")
        self.assertEqual(self.remaining_questions(), 9)

    def test_hit_completing_after_prebudget_retains_original_admission_time(self):
        real_hit = self.allowances._try_commit_locked

        def slower_hit(category):
            charged = real_hit(category)
            self.now = 120.0  # Past the pre-admission target but within the original 90 seconds.
            return charged

        with patch.object(self.allowances, "_try_commit_locked", side_effect=slower_hit):
            result = self.result()
        self.assert_result(result, "admitted", True, "admitted")
        self.assertEqual(result.admission.admitted_at, 100.0)
        self.assertEqual(result.admission.question_deadline, 190.0)
        self.assertEqual(self.remaining_questions(), 9)

    def test_preownership_read_holds_slot_without_claiming_conversation(self):
        started, finish = self.blocked_read(self.readers, "header")
        first = self.submit(executor=self.executor)
        self.assertTrue(started.wait(2))
        other = SimulatedReaders()
        self.assert_result(self.result(readers=other), "admitted", True, "admitted")
        first.withdraw()
        finish.set()
        self.assert_result(first.result(2), "stopped")
        self.assertEqual([call[0] for call in self.readers.calls], ["profile", "header"])

    def test_blocked_transcript_withdrawal_retains_key_until_read_and_cleanup_complete(self):
        started, finish = self.blocked_read(self.readers, "event")
        first = self.submit(executor=self.executor)
        self.assertTrue(started.wait(2))
        with self.assertRaises(TimeoutError):
            first.result(0)
        self.assertTrue(first.attempt.stopped)
        self.assert_result(self.result(readers=SimulatedReaders()), "conversation_busy")
        finish.set()
        self.assert_result(first.result(2), "stopped")
        self.executor.dispatched[0].result(2)  # Observe completion callbacks, not only wait wake-up.
        self.assert_result(self.result(readers=SimulatedReaders()), "admitted", True, "admitted")
        self.assertEqual(self.remaining_questions(), 9)

    def test_five_independent_ownership_reads_hold_capacity_after_withdrawal(self):
        readers_list, handles, finishes = [], [], []
        for _ in range(5):
            readers = SimulatedReaders()
            started, finish = self.blocked_read(readers, "profile")
            handle = self.submit(readers=readers, executor=self.executor)
            self.assertTrue(started.wait(2))
            handle.withdraw()
            readers_list.append(readers)
            handles.append(handle)
            finishes.append(finish)
        with self.assertRaises(admission.AdmissionBusy):
            self.submit()
        for finish, handle in zip(finishes, handles, strict=True):
            finish.set()
            self.assert_result(handle.result(2), "stopped")
        self.assert_result(self.result(), "admitted", True, "admitted")
        self.assertTrue(all(len(readers.calls) == 1 for readers in readers_list))

    def test_mutating_native_request_after_submit_cannot_rebind_work(self):
        request = QuestionRequest(user_id=USER_ID, conversation_id=CONVERSATION_ID, question="Question?")
        started, finish = self.blocked_read(self.readers, "profile")
        handle = self.submit(request=request, executor=self.executor)
        self.assertTrue(started.wait(2))
        request.user_id = "syn-cust-02"
        request.conversation_id = str(UUID(int=999))
        finish.set()
        self.assert_result(handle.result(2), "admitted", True, "admitted")
        self.assertEqual(self.readers.calls[1][1], CONVERSATION_ID)

    def test_dispatch_failure_after_start_withdraws_without_releasing_key(self):
        started, finish = self.blocked_read(self.readers, "event")
        with self.assertRaises(RuntimeError):
            self.submit(executor=SubmitThenRaise(self.executor, started=started))
        self.assert_result(self.result(readers=SimulatedReaders()), "conversation_busy")
        finish.set()
        self.executor.shutdown(wait=True)
        self.assert_result(self.result(readers=SimulatedReaders()), "admitted", True, "admitted")
        self.assertEqual(self.remaining_questions(), 9)

    def test_invalid_cap_fails_before_reads_and_custom_cap_changes_native_limit(self):
        for cap in (True, 0, -1, "50"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                self.submit(cap=cap)
        self.assertEqual(self.readers.calls, [])
        self.assert_result(self.result(cap=2), "admitted", True, "admitted")
        self.assertEqual(self.readers.calls[-1][3], 3)

    def test_confirmed_cancel_of_queued_work_prevents_reads_and_releases_slot(self):
        executor = ObservedThreadPool(max_workers=1)
        self.addCleanup(executor.shutdown, wait=True, cancel_futures=True)
        occupied, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def occupy():
            occupied.set()
            finish.wait(3)

        executor.submit(occupy)
        self.assertTrue(occupied.wait(2))
        queued = self.submit(executor=executor)
        self.assertTrue(queued.cancel())  # Actual callable prevention releases its lease.
        with self.assertRaises(CancelledError):
            queued.result(0)
        self.assertEqual(self.readers.calls, [])
        self.assertEqual(self.remaining_questions(), 10)
        finish.set()

    def test_running_cancel_holds_slot_and_key_through_separate_read_cleanup(self):
        read_started, finish_read = threading.Event(), threading.Event()
        cleanup_started, finish_cleanup = threading.Event(), threading.Event()
        self.addCleanup(finish_cleanup.set)
        self.addCleanup(finish_read.set)

        def read_then_cleanup():
            try:
                read_started.set()
                if not finish_read.wait(3):
                    raise AssertionError("The simulated read was not released.")
            finally:
                cleanup_started.set()
                if not finish_cleanup.wait(3):
                    raise AssertionError("The simulated cleanup was not released.")

        self.readers.hooks["event"] = read_then_cleanup
        first = self.submit(executor=self.executor)
        self.assertTrue(read_started.wait(2))
        self.assertFalse(first.cancel())  # Running work cannot be cancelled by native Future.
        finish_read.set()
        self.assertTrue(cleanup_started.wait(2))
        self.assert_result(self.result(readers=SimulatedReaders()), "conversation_busy")
        finish_others = threading.Event()
        self.addCleanup(finish_others.set)
        others = [self.slots.submit(self.executor, finish_others.wait, 3) for _ in range(4)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit(readers=SimulatedReaders())
        finish_cleanup.set()
        self.assert_result(first.result(2), "stopped")
        self.executor.dispatched[0].result(2)
        self.assertEqual(len(self.readers.calls), 3)  # No late offer read or question hit.
        self.assert_result(self.result(readers=SimulatedReaders()), "admitted", True, "admitted")
        finish_others.set()
        for lease in others:
            lease.result(2)
        self.assertEqual(self.remaining_questions(), 9)

    def test_owned_other_conversation_can_admit_while_first_transcript_is_blocked(self):
        started, finish = self.blocked_read(self.readers, "event")
        first = self.submit(executor=self.executor)
        self.assertTrue(started.wait(2))
        second_id = "00000000-0000-0000-0000-000000000009"
        from tests.verified_creation import verified_tracking
        verified_tracking(self.tracking, conversation_id=second_id)
        readers = SimulatedReaders()
        readers.header_value = header_result({**HEADER, "conversation_id": second_id})
        readers.header_value["ids"] = ["conversation_header:00000000000000000000000000000009"]
        readers.header_value["metadatas"][0]["conversation_id"] = second_id
        self.assert_result(self.result(readers=readers, request={
            "user_id": USER_ID, "conversation_id": second_id, "question": "Another question?",
        }), "admitted", True, "admitted")
        first.withdraw()
        finish.set()
        self.assert_result(first.result(2), "stopped")
        self.assertEqual(self.remaining_questions(), 9)

    def test_equivalent_uuid_spelling_contends_for_the_same_canonical_key(self):
        started, finish = self.blocked_read(self.readers, "event")
        first = self.submit(executor=self.executor)
        self.assertTrue(started.wait(2))
        self.assert_result(self.result(readers=SimulatedReaders(), request={
            "user_id": USER_ID, "conversation_id": "0123456789AB4CDE8FAB0123456789AB",
            "question": "Question?",
        }), "conversation_busy")
        first.withdraw()
        finish.set()
        self.assert_result(first.result(2), "stopped")
        self.assertEqual(self.remaining_questions(), 10)

    def test_dispatch_failure_after_completed_admission_retains_the_charge(self):
        with self.assertRaises(RuntimeError):
            self.submit(executor=SubmitThenRaise(InlineExecutor()))
        self.assert_result(self.result(readers=SimulatedReaders()), "admitted", True, "admitted")
        self.assertEqual(self.remaining_questions(), 8)  # Both native successful hits survive.

    def test_expiry_inside_reservation_prevents_events_and_quota(self):
        real_reserve = self.reservations.reserve

        def reserve_then_expire(*args, **kwargs):
            real_reserve(*args, **kwargs)  # Preserve the actual lease-owned reservation lifecycle.
            self.now = 115.0

        with patch.object(self.reservations, "reserve", side_effect=reserve_then_expire):
            self.assert_result(self.result(), "stopped")
        self.assertEqual([call[0] for call in self.readers.calls], ["profile", "header"])
        self.assertEqual(self.remaining_questions(), 10)

    def test_late_real_reservation_conflict_selects_stopped(self):
        # Returning busy without a fresh permission check would surface a late conflict.
        for stop in ("deadline", "withdrawal"):
            with self.subTest(stop=stop):
                self.now = 100.0
                owner_readers = SimulatedReaders()
                owner_started, finish_owner = self.blocked_read(owner_readers, "event")
                owner = self.submit(readers=owner_readers, executor=self.executor)
                owner_dispatch = self.executor.dispatched[-1]
                self.assertTrue(owner_started.wait(2))
                conflict_started, finish_conflict = threading.Event(), threading.Event()
                self.addCleanup(finish_conflict.set)
                real_reserve = self.reservations.reserve

                def conflict_then_pause(*args, **kwargs):
                    try:
                        return real_reserve(*args, **kwargs)  # Preserve actual registry conflict.
                    except admission.ReservationConflict:
                        conflict_started.set()
                        if not finish_conflict.wait(3):
                            raise AssertionError("The test did not release conflict classification.")
                        raise

                readers = SimulatedReaders()
                try:
                    with patch.object(self.reservations, "reserve", side_effect=conflict_then_pause):
                        contender = self.submit(readers=readers, executor=self.executor)
                        self.assertTrue(conflict_started.wait(2))
                        if stop == "deadline":
                            self.now = 115.0
                        else:
                            contender.withdraw()
                        finish_conflict.set()
                        self.assert_result(contender.result(2), "stopped")
                    self.assertEqual([call[0] for call in readers.calls], ["profile", "header"])
                finally:
                    finish_conflict.set()
                    owner.withdraw()
                    finish_owner.set()
                    self.assert_result(owner.result(2), "stopped")
                    owner_dispatch.result(2)  # Registry callbacks must finish before the next case.
        self.assertEqual(self.remaining_questions(), 10)

    def test_full_and_overlap_precede_even_exhausted_question_allowance(self):
        for _ in range(10):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.readers.event_value = event_result(*transcript_pair())
        self.assert_result(self.result(cap=2), "conversation_full")
        started, finish = self.blocked_read(self.readers, "event")
        first = self.submit(executor=self.executor)
        self.assertTrue(started.wait(2))
        self.assert_result(self.result(readers=SimulatedReaders()), "conversation_busy")
        first.withdraw()
        finish.set()
        self.assert_result(first.result(2), "stopped")

    def test_mutated_invalid_native_model_is_revalidated_before_any_read(self):
        request = QuestionRequest(user_id=USER_ID, conversation_id=CONVERSATION_ID, question="Question?")
        request.question = "   "  # Native model mutation is legal; admission must revalidate it.
        with self.assertRaises(ValidationError):
            self.submit(request=request)
        self.assertEqual(self.readers.calls, [])

    def test_early_withdrawal_during_profile_validation_selects_stopped(self):
        started, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        self.readers.profile_value = event_result()  # A real absent result, not preverified flags.
        real_verifier = admission.verify_profile

        def validate_then_block(*args, **kwargs):
            checked = real_verifier(*args, **kwargs)
            started.set()
            if not finish.wait(3):
                raise AssertionError("The test did not release validation.")
            return checked

        with patch.object(admission, "verify_profile", side_effect=validate_then_block):
            handle = self.submit(executor=self.executor)
            self.assertTrue(started.wait(2))
            handle.withdraw()
            finish.set()
            self.assert_result(handle.result(2), "stopped")
        self.assertEqual(len(self.readers.calls), 1)
        self.assertEqual(self.remaining_questions(), 10)


# Overall flow: complete native-shaped doubles -> real verifier/lease/allowance coordinator
# -> externally observable safe decisions, charge, storage ordering, and tracked lifetimes.
