# Python 3.12; real attempt/record validation with controlled storage outcomes.
import importlib.util
import unittest
from threading import Lock
from limits.storage import MemoryStorage
from shopunow.admission import QuestionAttempt, OwnedRecordTracking
from shopunow.allowances import RollingAllowances
from tests.test_records import (USER_ID, CONVERSATION_ID, REQUEST_ID, OFFER_ID,
                               offer_result, marker_result)


class FallbackBoundary:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail
        self.offer, self.marker = offer_result(), marker_result()
        self.offer['metadatas'][0].update(reason_code='security_uncertain',
                                         target_department_or_fallback='human_support')

    def call(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise TimeoutError('controlled external failure')

    def write_offer(self, offer, *, timeout):
        self.call('offer_write')

    def exact_offer(self, offer_id, *, timeout):
        self.call('offer_read')
        return self.offer

    def write_preclearance_marker(self, marker, *, timeout):
        self.call('marker_write')

    def exact_preclearance_marker(self, request_id, *, timeout):
        self.call('marker_read')
        return self.marker


class PreclearanceRecorderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.preclearance'),
                             'Exact-verified fallback recorder is missing')
        self.module = importlib.import_module('shopunow.preclearance')
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.attempt = QuestionAttempt(100, clock=lambda: 100)
        self.attempt.try_admit(RollingAllowances(self.storage, Lock()))
        self.tracking = OwnedRecordTracking()

    def record(self, boundary):
        return self.module.record_preclearance_fallback(
            boundary, attempt=self.attempt, tracking=self.tracking,
            user_id=USER_ID, conversation_id=CONVERSATION_ID,
            request_id=REQUEST_ID, offer_id=OFFER_ID, reason='security_uncertain',
            created_at='2026-11-02T10:00:00Z')

    def test_display_requires_offer_then_marker_then_linked_exact_reverification(self):
        boundary = FallbackBoundary()
        result = self.record(boundary)
        self.assertEqual(result.offer_id, OFFER_ID)
        self.assertEqual(boundary.calls, ['offer_write', 'offer_read', 'marker_write',
                                          'marker_read', 'offer_read'])
        self.assertFalse(self.tracking.excludes((CONVERSATION_ID,)))

    def test_unknown_write_has_no_offer_and_preserves_conversation_exclusion(self):
        for failure in ('offer_write', 'offer_read', 'marker_write', 'marker_read'):
            with self.subTest(failure=failure):
                self.tracking = OwnedRecordTracking()  # Each scenario must reach its own failure.
                boundary = FallbackBoundary(failure)
                self.assertIsNone(self.record(boundary).offer_id)
                self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))
                order = ['offer_write', 'offer_read', 'marker_write', 'marker_read']
                self.assertEqual(boundary.calls, order[:order.index(failure) + 1])

    def test_conflicting_marker_withholds_offer_and_holds_conversation(self):
        boundary = FallbackBoundary()
        boundary.marker['documents'][0] = 'untrusted question text'
        self.assertIsNone(self.record(boundary).offer_id)
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))

    def test_stopped_attempt_never_starts_a_write(self):
        self.attempt.withdraw()
        boundary = FallbackBoundary()
        self.assertIsNone(self.record(boundary).offer_id)
        self.assertEqual(boundary.calls, [])
        self.assertFalse(self.tracking.excludes((CONVERSATION_ID,)))

    def test_withdrawal_during_intent_retention_never_starts_writer(self):
        # An injected retention boundary models a withdrawal during its guard wait.
        real_tracking, attempt = self.tracking, self.attempt
        class WithdrawalDuringRetention:
            def retain_preclearance(self, intent):
                real_tracking.retain_preclearance(intent)
                attempt.withdraw()

            def exclude_conversation(self, conversation_id):
                real_tracking.exclude_conversation(conversation_id)
        self.tracking = WithdrawalDuringRetention()
        boundary = FallbackBoundary()
        self.assertIsNone(self.record(boundary).offer_id)
        self.assertEqual(boundary.calls, [])
        self.assertFalse(real_tracking.excludes((CONVERSATION_ID,)))

    def test_process_control_failure_after_writer_entry_preserves_hold_and_propagates(self):
        class InterruptedWrite(FallbackBoundary):
            def write_offer(self, offer, *, timeout):
                self.calls.append('offer_write')
                raise KeyboardInterrupt('controlled possible commit')
        boundary = InterruptedWrite()
        with self.assertRaises(KeyboardInterrupt):
            self.record(boundary)
        self.assertEqual(boundary.calls, ['offer_write'])
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))

    def test_original_question_deadline_bounds_authorized_work_timeout(self):
        clock = [100.0]
        attempt = QuestionAttempt(clock[0], clock=lambda: clock[0])
        attempt.try_admit(RollingAllowances(self.storage, Lock()))
        timeout = getattr(attempt, 'normal_work_timeout', None)
        self.assertTrue(callable(timeout), 'Guarded question work timeout is missing')
        clock[0] = 189.75  # Only a quarter second remains from original admission.
        self.assertEqual(timeout(), 0.25)
        clock[0] = 190.0
        self.assertIsNone(timeout())
        self.assertFalse(attempt.authorize_normal_work())


# Flow: admitted real attempt -> ordered storage boundaries -> exact pair -> display or hold.
