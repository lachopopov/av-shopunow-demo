# Python 3.12; simulated native storage, real record/admission/allowance logic (Slice 6 handoff form).
import json
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID
from limits.storage import MemoryStorage
from shopunow import admission
from shopunow.allowances import Allowance, RollingAllowances
from tests.test_admission import InlineExecutor
from tests.test_records import (CONVERSATION_ID, OFFER_ID, REQUEST_ID, TERMINAL_METADATA, USER_ID,
                                offer_result, profile_result, terminal_result)

RID = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
EMPTY = {'ids': [], 'metadatas': [], 'documents': [], 'embeddings': None, 'uris': None, 'data': None}
FORM = {'user_id': USER_ID, 'conversation_id': CONVERSATION_ID, 'offer_id': OFFER_ID,
        'name': 'Sam Synthetic', 'email': 'sam@example.test'}


def linked_terminal():
    # A saved completed_offer outcome for the offer's own request, as Slice 5 writes it.
    return terminal_result(document='Our support team can help with this. You can request a support handoff.',
                           metadata=dict(TERMINAL_METADATA, kind='completed_offer', verified_offer_id=OFFER_ID))


class HandoffStorage:
    """Escalations/Conversations doubles with immutable add semantics and controllable reads."""

    def __init__(self):
        self.calls, self.hooks = [], {}
        self.profile_data = profile_result()
        self.offer_data = offer_result()
        self.terminal_data = linked_terminal()
        self.marker_data = deepcopy(EMPTY)
        self.saved = {}  # submission record ID -> (document, metadata)
        self.readback = None  # Optional replacement for the next submission read.
        self.tracking = None

    def step(self, kind, timeout):
        self.calls.append((kind, timeout))
        if kind in self.hooks:
            self.hooks[kind]()

    def profile(self, user_id, *, timeout):
        self.step('profile', timeout)
        return self.profile_data

    def exact_offer(self, offer_id, *, timeout):
        self.step('offer', timeout)
        return self.offer_data

    def exact_terminal(self, request_id, *, timeout):
        self.step('terminal', timeout)
        return self.terminal_data

    def exact_preclearance_marker(self, request_id, *, timeout):
        self.step('marker', timeout)
        return self.marker_data

    def exact_submission(self, offer_id, *, timeout):
        self.step('submission', timeout)
        if self.readback is not None:
            value, self.readback = self.readback, None
            return value
        key = 'submission:' + UUID(offer_id).hex
        if key not in self.saved:
            return deepcopy(EMPTY)
        document, metadata = self.saved[key]
        return {'ids': [key], 'metadatas': [dict(metadata)], 'documents': [document]}

    def write_submission(self, submission, *, timeout):
        if not self.tracking.submission_unresolved(OFFER_ID):
            raise AssertionError('Possible submission not tracked before writer entry')
        self.step('write_submission', timeout)
        key = 'submission:' + UUID(str(submission.offer_id)).hex
        self.saved.setdefault(key, (submission.model_dump_json(), {
            'record_type': 'submission', 'offer_id': str(submission.offer_id),
            'owner_user_id': submission.owner_user_id, 'conversation_id': submission.conversation_id}))


class HandoffSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.slots = admission.ExecutionSlots()
        self.reservations = admission.ResourceReservations()
        self.native = MemoryStorage()
        self.addCleanup(self.native.reset)
        self.allowances = RollingAllowances(self.native, threading.Lock())
        self.storage = HandoffStorage()
        self.tracking = admission.OwnedRecordTracking()
        self.storage.tracking = self.tracking
        self.now = 100.0

    def submit(self, form=None, **options):
        return admission.submit_handoff({**(form or FORM), 'request_id': RID}, slots=self.slots,
            reservations=self.reservations, allowances=self.allowances,
            executor=options.pop('executor', InlineExecutor()), storage=self.storage,
            tracking=self.tracking, clock=lambda: self.now, utc_now=lambda: NOW, **options).result()

    def writes(self):
        return [kind for kind, _ in self.storage.calls].count('write_submission')

    def remaining_writes(self):
        return sum(self.allowances.try_commit(Allowance.WRITE) for _ in range(11))

    def test_a_valid_form_gets_a_receipt_only_after_its_saved_record_reads_back_exactly(self):
        # Break caught: a receipt from the write acknowledgement alone, or a record missing form data.
        result = self.submit()
        self.assertEqual(result.status, 'ready')
        response = result.response
        self.assertEqual((response.user_id, response.conversation_id, response.request_id),
                         (USER_ID, CONVERSATION_ID, RID))
        self.assertEqual((response.contact_method, response.simulated), ('email', True))
        self.assertEqual(self.writes(), 1)
        kinds = [kind for kind, _ in self.storage.calls]
        self.assertGreater(len(kinds) - 1 - kinds[::-1].index('submission'), kinds.index('write_submission'))
        document = json.loads(next(iter(self.storage.saved.values()))[0])
        self.assertEqual((document['name'], document['email'], document['phone'], document['reason_code'],
                          document['target'], document['receipt_id']),
                         ('Sam Synthetic', 'sam@example.test', None, 'individual_case', 'shipping_delivery',
                          response.receipt_id))
        self.assertFalse(self.tracking.submission_unresolved(OFFER_ID))
        self.assertEqual(self.remaining_writes(), 9)  # Exactly one WRITE allowance was used.

    def test_phone_only_form_reports_phone_as_the_contact_method(self):
        result = self.submit({k: v for k, v in FORM.items() if k != 'email'} | {'phone': '+44 20 7946 0000'})
        self.assertEqual(result.response.contact_method, 'phone')

    def test_an_identical_repeat_returns_the_same_receipt_without_writing_again(self):
        # Break caught: a second record, or a new receipt for the same submitted form.
        first = self.submit().response
        second = self.submit().response
        self.assertEqual((second.submission_id, second.receipt_id), (first.submission_id, first.receipt_id))
        self.assertEqual(self.writes(), 1)

    def test_a_conflicting_repeat_is_rejected_and_never_overwrites_the_saved_record(self):
        # Break caught: overwrite, or a receipt for different contact details.
        self.submit()
        saved = dict(self.storage.saved)
        result = self.submit(FORM | {'name': 'Someone Else'})
        self.assertEqual((result.status, result.response), ('handoff_conflict', None))
        self.assertEqual((self.writes(), self.storage.saved), (1, saved))

    def test_an_unverified_write_gives_no_receipt_and_holds_the_offer(self):
        # Break caught: a receipt or a blind retry while the earlier write may still commit.
        self.storage.hooks['write_submission'] = lambda: setattr(self.storage, 'readback', deepcopy(EMPTY))
        result = self.submit()
        self.assertEqual((result.status, result.response), ('unresolved', None))
        self.assertTrue(self.tracking.submission_unresolved(OFFER_ID))
        self.storage.hooks.clear()
        self.assertEqual(self.submit().status, 'handoff_busy')
        self.assertEqual(self.writes(), 1)

    def test_a_failed_write_call_also_holds_the_offer(self):
        def fail():
            raise TimeoutError('write outcome unknown')
        self.storage.hooks['write_submission'] = fail
        self.assertEqual(self.submit().status, 'unresolved')
        self.assertTrue(self.tracking.submission_unresolved(OFFER_ID))

    def test_missing_foreign_or_other_conversation_offers_get_the_same_safe_failure(self):
        # Break caught: a receipt for someone else's offer, or an error that reveals the offer exists.
        cases = {'missing': deepcopy(EMPTY),
                 'foreign': offer_result() | {'metadatas': [dict(offer_result()['metadatas'][0],
                                                                 owner_user_id='syn-cust-02')]},
                 'other_conversation': offer_result() | {'metadatas': [dict(
                     offer_result()['metadatas'][0], conversation_id='22222222-2222-4222-8222-222222222222')]}}
        for name, data in cases.items():
            with self.subTest(name):
                self.storage.offer_data = data
                result = self.submit()
                self.assertEqual((result.status, result.charged), ('offer_unavailable', False))
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.remaining_writes(), 10)  # Failures before the hit consume no allowance.

    def test_an_offer_without_a_verified_link_cannot_be_submitted(self):
        # Break caught: an offer whose terminal link failed (LLD §4: unusable until reconciliation).
        cases = {'no_terminal': deepcopy(EMPTY),
                 'other_offer': terminal_result(document='x', metadata=dict(
                     TERMINAL_METADATA, kind='completed_offer',
                     verified_offer_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')),
                 'technical': terminal_result(document='technical_error', metadata=dict(
                     TERMINAL_METADATA, kind='failed_technical'))}
        for name, data in cases.items():
            with self.subTest(name):
                self.storage.terminal_data = data
                self.assertEqual(self.submit().status, 'offer_unavailable')
        self.assertEqual(self.writes(), 0)

    def test_a_preclearance_fallback_offer_can_be_submitted(self):
        # Break caught: fallback offers shown in history notices cannot be used.
        self.storage.terminal_data = deepcopy(EMPTY)
        self.storage.marker_data = {
            'ids': ['preclearance_terminal:' + UUID(REQUEST_ID).hex],
            'metadatas': [{'record_type': 'preclearance_terminal',
                           'marker_id': 'preclearance_terminal:' + UUID(REQUEST_ID).hex,
                           'request_id': REQUEST_ID, 'owner_user_id': USER_ID, 'conversation_id': CONVERSATION_ID,
                           'verified_offer_id': OFFER_ID, 'reason_code': 'security_uncertain',
                           'created_at': '2026-11-02T10:00:00Z', 'complete': True}],
            'documents': ['preclearance_terminal']}
        self.assertEqual(self.submit().status, 'ready')

    def test_a_busy_offer_or_conversation_is_rejected_without_a_write(self):
        # Break caught: two overlapping writes for one offer, or a write during a question in the chat.
        for resource in (('offer', OFFER_ID), ('conversation', CONVERSATION_ID)):
            with self.subTest(resource):
                gate = threading.Event()
                from concurrent.futures import ThreadPoolExecutor
                pool = ThreadPoolExecutor(1)
                self.addCleanup(pool.shutdown, wait=True)
                started = threading.Event()

                def hold(lease, resource=resource):
                    self.reservations.reserve(lease, resource)
                    started.set()
                    gate.wait(5)
                lease = self.slots.submit_with_lease(pool, hold)
                started.wait(5)
                try:
                    self.assertEqual(self.submit().status, 'handoff_busy')
                finally:
                    gate.set()
                    lease.result(5)
        self.assertEqual(self.writes(), 0)

    def test_an_excluded_conversation_rejects_the_form_before_any_write_or_charge(self):
        # Break caught (review S1): contact data saved, then the receipt refused, for a conversation
        # whose earlier write is unresolved.
        self.tracking.exclude_conversation(CONVERSATION_ID)
        result = self.submit()
        self.assertEqual((result.status, result.charged), ('conversation_unresolved', False))
        self.assertEqual((self.writes(), self.storage.saved), (0, {}))

    def test_a_write_never_starts_without_time_for_its_read_back(self):
        # Break caught (review M1): a write started with a fraction of a second left times out, and the
        # offer stays held for the rest of the process.
        def late():
            self.now += 20.0 - admission.SUBMISSION_WRITE_FLOOR + 0.5
        self.storage.hooks['submission'] = late
        self.assertEqual(self.submit().status, 'stopped')
        self.assertEqual(self.writes(), 0)
        self.assertFalse(self.tracking.submission_unresolved(OFFER_ID))

    def test_a_saved_write_that_outlasts_the_deadline_is_still_read_back_and_releases_the_offer(self):
        # Break caught (challenger D1): the write uses all remaining time, the read-back is skipped, and a
        # correctly saved form leaves the offer held ("busy") for the rest of the process.
        def slow_write():
            self.now += 20.0  # The deadline passes while the write runs; the record is still saved.
        self.storage.hooks['write_submission'] = slow_write
        result = self.submit()
        self.assertEqual((result.status, result.response), ('stopped', None))  # No late receipt.
        self.assertEqual(self.writes(), 1)
        self.assertFalse(self.tracking.submission_unresolved(OFFER_ID))
        self.storage.hooks.clear()
        self.assertEqual(self.submit().status, 'ready')  # The identical repeat now gets its receipt.
        self.assertEqual(self.writes(), 1)

    def test_a_withdrawal_during_the_write_still_reads_back_and_releases_the_offer(self):
        # Break caught (challenger D2): a disconnect during the write skips the read-back; offer held.
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()
        def blocking_write():
            entered.set()
            release.wait(5)
        self.storage.hooks['write_submission'] = blocking_write
        pool = ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown, wait=True)
        handle = admission.submit_handoff({**FORM, 'request_id': RID}, slots=self.slots,
            reservations=self.reservations, allowances=self.allowances, executor=pool, storage=self.storage,
            tracking=self.tracking, clock=lambda: self.now, utc_now=lambda: NOW)
        self.assertTrue(entered.wait(5))
        handle.withdraw()
        release.set()
        result = handle.lease.result(5)
        self.assertEqual((result.status, result.response), ('stopped', None))
        self.assertIn('submission', [kind for kind, _ in self.storage.calls][-1:])  # Read-back happened last.
        self.assertFalse(self.tracking.submission_unresolved(OFFER_ID))

    def test_an_absent_read_back_after_a_stop_keeps_the_offer_held(self):
        # Break caught: the cleanup read-back clears the hold without finding the exact saved record.
        def slow_lost_write():
            self.now += 20.0
            self.storage.readback = deepcopy(EMPTY)
        self.storage.hooks['write_submission'] = slow_lost_write
        self.assertEqual(self.submit().status, 'stopped')
        self.assertTrue(self.tracking.submission_unresolved(OFFER_ID))

    def test_the_fallback_marker_is_read_only_when_no_terminal_exists(self):
        self.submit()
        self.assertNotIn('marker', [kind for kind, _ in self.storage.calls])

    def test_an_identical_repeat_needs_a_stored_record_that_matches_its_offer(self):
        # Break caught (review M2): a receipt from a stored record whose submission ID, reason, target or
        # metadata do not belong to this offer.
        self.submit()
        key, (document, metadata) = next(iter(self.storage.saved.items()))
        for field, value in (('submission_id', '99999999-9999-4999-8999-999999999999'),
                             ('reason_code', 'other_reason'), ('target', 'hr')):
            with self.subTest(field):
                changed = json.loads(document) | {field: value}
                self.storage.saved[key] = (json.dumps(changed), metadata)
                self.assertNotEqual(self.submit().status, 'ready')
        for field, value in (('record_type', 'offer'), ('conversation_id', '22222222-2222-4222-8222-222222222222')):
            with self.subTest(field):
                self.storage.saved[key] = (document, dict(metadata, **{field: value}))
                self.assertNotEqual(self.submit().status, 'ready')

    def test_exhausted_write_allowance_rejects_before_any_write(self):
        while self.allowances.try_commit(Allowance.WRITE):
            pass
        result = self.submit()
        self.assertEqual((result.status, result.charged), ('quota_exhausted', False))
        self.assertEqual(self.writes(), 0)

    def test_a_deadline_before_the_write_stops_without_writing(self):
        # Break caught: work after the 20-second handoff budget expired.
        def expire():
            self.now += 20.0
        self.storage.hooks['submission'] = expire
        self.assertEqual(self.submit().status, 'stopped')
        self.assertEqual(self.writes(), 0)

    def test_the_handoff_budget_is_twenty_seconds(self):
        # Break caught: the 15-second budget of other writes applied to the slower handoff path (MLD §11).
        def late():
            self.now += 15.5  # Past 15 s, still above the write floor.
        self.storage.hooks['offer'] = late
        self.assertEqual(self.submit().status, 'ready')



class SubmissionSDKTests(unittest.TestCase):
    """Actual SDKRecords add/get mapping for a submission, with a controlled collection boundary."""

    def test_submission_round_trips_with_contact_only_in_the_document(self):
        # Break caught: contact data in filterable metadata, or a mapping the verifier cannot read back.
        from types import SimpleNamespace
        from shopunow.records import HandoffSubmission, verify_submission, verify_submittable_offer
        from shopunow.sdk_writes import SDKRecords
        from tests.incoming_fixture import StoredCollection
        from tests.test_preclearance_writes import ImmediateBridge
        owner = SimpleNamespace(storage_calls=[], storage_hook=lambda *args: None)
        escalations = StoredCollection('escalations', owner)
        records = SDKRecords(ImmediateBridge(), profiles=None, conversations=None, escalations=escalations)
        offer = verify_submittable_offer(offer_result(), user_id=USER_ID, conversation_id=CONVERSATION_ID,
                                         offer_id=OFFER_ID).record
        from shopunow.records import submission_id_for
        intended = HandoffSubmission(record_type='submission', submission_id=submission_id_for(OFFER_ID),
            receipt_id='44444444-4444-4444-8444-444444444444', owner_user_id=USER_ID,
            conversation_id=CONVERSATION_ID, offer_id=OFFER_ID, request_reference=REQUEST_ID,
            reason_code='individual_case', target='shipping_delivery', name='Sam Synthetic', phone=None,
            email='sam@example.test', simulated_review=True, complete=True, created_at=NOW)
        records.write_submission(intended, timeout=2)
        _, _, payload = owner.storage_calls[0]
        self.assertEqual(payload['ids'], ['submission:' + UUID(OFFER_ID).hex])
        self.assertNotIn('sam@example.test', json.dumps(payload['metadatas']))
        self.assertEqual(verify_submission(records.exact_submission(OFFER_ID, timeout=2), offer=offer).record,
                         intended)


if __name__ == '__main__':
    unittest.main()
