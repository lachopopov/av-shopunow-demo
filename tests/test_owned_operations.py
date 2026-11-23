# Python 3.12; simulated native storage, real record/admission/allowance logic.
import json
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID
from limits.storage import MemoryStorage
from shopunow import admission, api
from shopunow.allowances import Allowance, RollingAllowances
from tests.test_admission import InlineExecutor, ObservedThreadPool
from tests.test_records import USER_ID, CONVERSATION_ID, HEADER, header_result, profile_result
from tests.test_identity_reads import EMPTY, preference_result

RID = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
SECOND = '22222222-2222-4222-8222-222222222222'
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def headers_result(ids=(CONVERSATION_ID, SECOND)):
    rows = [header_result(dict(HEADER, conversation_id=value)) for value in ids]
    return {'ids': ['conversation_header:'+UUID(value).hex for value in ids],
            'metadatas': [dict(row['metadatas'][0], conversation_id=value) for row, value in zip(rows, ids)],
            'documents': [row['documents'][0] for row in rows],
            'included': ['metadatas', 'documents'], 'embeddings': None, 'uris': None, 'data': None}


class OwnedStorage:
    def __init__(self):
        self.calls, self.hooks = [], {}
        self.profile_data = profile_result()
        self.header_data = header_result()
        self.headers_data = headers_result()
        self.preference_data = deepcopy(EMPTY)
        self.saved_header = self.saved_preference = None
        self.tracking = None

    def step(self, kind, timeout):
        self.calls.append((kind, timeout))
        if kind in self.hooks:
            self.hooks[kind]()

    def profile(self, user_id, *, timeout):
        self.step('profile', timeout)
        return self.profile_data

    def header(self, conversation_id, *, timeout):
        self.step('header', timeout)
        if self.saved_header is None:
            return self.header_data
        value = self.saved_header.model_dump(mode='json')
        return {'ids': ['conversation_header:'+UUID(conversation_id).hex],
                'metadatas': [{'record_type': 'conversation_header', 'owner_user_id': USER_ID,
                               'conversation_id': conversation_id}],
                'documents': [json.dumps(value)]}

    def headers(self, user_id, *, timeout):
        self.step('headers', timeout)
        return self.headers_data

    def preference(self, user_id, *, timeout):
        self.step('preference', timeout)
        return self.preference_data

    def write_header(self, header, *, timeout):
        if not self.tracking.excludes((str(header.conversation_id),)):
            raise AssertionError('Possible creation not excluded before writer entry')
        self.saved_header = header
        self.step('write_header', timeout)

    def write_preference(self, preference, *, timeout):
        if not self.tracking.preference_unresolved(USER_ID):
            raise AssertionError('Possible preference update not tracked before writer entry')
        self.saved_preference = preference
        self.step('write_preference', timeout)
        self.preference_data = {'ids': ['preference:'+USER_ID+':response_style'],
            'metadatas': [{'record_type': 'preference', 'owner_user_id': USER_ID, 'key': 'response_style'}],
            'documents': [preference.model_dump_json()]}


class OwnedOperationTests(unittest.TestCase):
    def setUp(self):
        self.slots = admission.ExecutionSlots()
        self.reservations = admission.ResourceReservations()
        self.native = MemoryStorage()
        self.addCleanup(self.native.reset)
        self.allowances = RollingAllowances(self.native, threading.Lock())
        self.storage = OwnedStorage()
        self.now = 100.0

    def setup_tracking(self):
        self.assertTrue(callable(getattr(admission, 'OwnedRecordTracking', None)),
                        'Missing local shared owned-record tracking')
        tracking = admission.OwnedRecordTracking()
        self.storage.tracking = tracking
        return tracking

    def create(self, tracking, **options):
        return admission.submit_conversation_creation(user_id=USER_ID, request_id=RID,
            slots=self.slots, reservations=self.reservations, allowances=self.allowances,
            executor=options.pop('executor', InlineExecutor()), storage=self.storage,
            supervisor=tracking, tracking=tracking, clock=lambda:self.now,
            uuid_factory=lambda: UUID(CONVERSATION_ID), utc_now=lambda:NOW, **options)

    def listing(self, tracking):
        return admission.submit_conversation_list({'user_id':USER_ID, 'request_id':RID},
            slots=self.slots, allowances=self.allowances, executor=InlineExecutor(),
            readers=self.storage, tracking=tracking, clock=lambda:self.now)

    def update(self, tracking, **options):
        return admission.submit_preference_update({'user_id':USER_ID, 'request_id':RID,
            'key':'response_style', 'value':'brief'}, slots=self.slots,
            reservations=self.reservations, allowances=self.allowances,
            executor=options.pop('executor', InlineExecutor()), storage=self.storage,
            tracking=tracking, clock=lambda:self.now, utc_now=lambda:NOW, **options)

    def remaining(self, category):
        return sum(self.allowances.try_commit(category) for _ in range(31))

    def test_verified_creation_is_eligible_despite_retained_intent(self):
        tracking = self.setup_tracking()
        handle = self.create(tracking)
        self.assertEqual(handle.result().status, 'ready')
        handle.attempt.bind_response(api.project_creation_response(handle.result()))
        self.assertFalse(tracking.excludes((CONVERSATION_ID,)))
        self.assertEqual(handle.attempt.checked_state().code, None)
        tracking.exclude_conversation(CONVERSATION_ID)
        self.assertEqual(handle.attempt.checked_state().code, 'conversation_unresolved')
        self.assertEqual(self.remaining(Allowance.WRITE), 9)

    def test_unverified_creation_completion_excludes_only_its_id(self):
        tracking = self.setup_tracking()
        self.storage.hooks['header'] = lambda: setattr(self.storage, 'saved_header', None)
        self.storage.header_data = deepcopy(EMPTY)
        result = self.create(tracking).result()
        self.assertEqual(result.status, 'unresolved')
        self.assertTrue(tracking.excludes((CONVERSATION_ID,)))
        result = self.listing(tracking).result()
        self.assertEqual([row.conversation_id for row in result.response.conversations], [SECOND])
        self.assertTrue(tracking.excludes((CONVERSATION_ID,)))
        self.assertEqual(self.remaining(Allowance.HISTORY), 29)

    def test_retention_failure_prevents_writer_call(self):
        tracking = self.setup_tracking()
        def failed(intent):
            raise RuntimeError('private retention fault')
        tracking.retain = failed
        self.assertEqual(self.create(tracking).result().status, 'unresolved')
        self.assertNotIn('write_header', [row[0] for row in self.storage.calls])

    def test_listing_checks_all_headers_before_excluding_any(self):
        tracking = self.setup_tracking()
        tracking.exclude_conversation(CONVERSATION_ID)
        for fault in ['foreign', 'incomplete', 'wrong_id', 'duplicate', 'shape']:
            with self.subTest(fault=fault):
                native = headers_result()
                document = json.loads(native['documents'][0])
                if fault == "foreign":
                    document["owner_user_id"] = "syn-cust-02"
                if fault == "incomplete":
                    document["complete"] = False
                native['documents'][0] = json.dumps(document)
                if fault == "wrong_id":
                    native["ids"][0] = "wrong"
                if fault == "duplicate":
                    native = headers_result((CONVERSATION_ID, CONVERSATION_ID))
                if fault == "shape":
                    native["metadatas"] = []
                self.storage.headers_data = native
                result = self.listing(tracking).result()
                self.assertEqual(result.status, 'unresolved')
                self.assertIsNone(result.response)
        self.assertEqual(self.remaining(Allowance.HISTORY), 30)

    def test_listing_empty_counts_without_event_or_offer_scans(self):
        tracking = self.setup_tracking()
        self.storage.headers_data = deepcopy(EMPTY)
        self.assertEqual(self.listing(tracking).result().response.conversations, ())
        self.assertEqual([row[0] for row in self.storage.calls], ['profile', 'headers'])
        self.assertEqual(self.remaining(Allowance.HISTORY), 29)

    def test_listing_does_not_apply_transcript_cap_to_header_count(self):
        tracking = self.setup_tracking()
        ids = tuple(str(UUID(int=index+1)) for index in range(60))
        self.storage.headers_data = headers_result(ids)
        self.assertEqual(len(self.listing(tracking).result().response.conversations), 60)

    def test_first_and_next_preference_versions_are_verified(self):
        tracking = self.setup_tracking()
        self.assertEqual(self.update(tracking).result().response.value, 'brief')
        self.assertEqual(self.storage.saved_preference.version, 1)
        self.assertEqual(self.update(tracking).result().response.value, 'brief')
        self.assertEqual(self.storage.saved_preference.version, 2)
        self.assertFalse(tracking.preference_unresolved(USER_ID))
        self.assertEqual(self.remaining(Allowance.WRITE), 8)

    def test_wrong_readback_retains_preference_hold_after_completion(self):
        tracking = self.setup_tracking()
        original = self.storage.preference
        calls = []
        def read(user_id, *, timeout):
            calls.append(None)
            if len(calls) > 1:
                return deepcopy(EMPTY)
            return original(user_id, timeout=timeout)
        self.storage.preference = read
        result = self.update(tracking).result()
        self.assertEqual(result.status, 'unresolved')
        self.assertTrue(tracking.preference_unresolved(USER_ID))
        self.assertEqual(self.update(tracking).result().status, 'preference_busy')
        self.assertEqual([r[0] for r in self.storage.calls].count('write_preference'), 1)
        self.assertEqual(self.remaining(Allowance.WRITE), 9)

    def test_invalid_current_preference_does_not_write_or_charge(self):
        tracking = self.setup_tracking()
        self.storage.preference_data = preference_result("invalid")
        result = self.update(tracking).result()
        self.assertEqual(result.status, 'unresolved')
        self.assertFalse(tracking.preference_unresolved(USER_ID))
        self.assertNotIn('write_preference', [row[0] for row in self.storage.calls])
        self.assertEqual(self.remaining(Allowance.WRITE), 10)

    def test_preference_retention_failure_never_enters_writer(self):
        tracking = self.setup_tracking()
        def failed(intent): raise RuntimeError('private fault')
        tracking.retain_preference = failed
        self.assertEqual(self.update(tracking).result().status, 'unresolved')
        self.assertNotIn('write_preference', [row[0] for row in self.storage.calls])

    def test_preference_readback_checks_every_intended_field(self):
        tracking = self.setup_tracking()
        def corrupt_readback():
            if self.storage.saved_preference is not None:
                changed = self.storage.saved_preference.model_dump(mode='json')
                changed['version'] = 99
                self.storage.preference_data = {'ids':['preference:'+USER_ID+':response_style'],
                    'metadatas':[{'record_type':'preference','owner_user_id':USER_ID,'key':'response_style'}],
                    'documents':[json.dumps(changed)]}
        self.storage.hooks['preference'] = corrupt_readback
        self.assertEqual(self.update(tracking).result().status, 'unresolved')
        self.assertTrue(tracking.preference_unresolved(USER_ID))

    def test_shared_user_reservation_prevents_creation_during_preference_write(self):
        tracking = self.setup_tracking()
        started = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        def blocked():
            started.set()
            if not finish.wait(3):
                raise RuntimeError("test barrier")

        self.storage.hooks['write_preference'] = blocked
        executor = ObservedThreadPool(max_workers=2)
        self.addCleanup(executor.shutdown, wait=True, cancel_futures=True)
        handle = self.update(tracking, executor=executor)
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(self.create(tracking).result().status, 'creation_busy')
            self.assertEqual(self.update(tracking).result().status, 'preference_busy')
        finally:
            finish.set()
        self.assertEqual(handle.result(2).status, 'ready')
        self.assertEqual(self.remaining(Allowance.WRITE), 9)

    def test_final_boundary_precedence_preserves_verified_save(self):
        tracking = self.setup_tracking()
        handle = self.create(tracking)
        handle.attempt.bind_response(api.project_creation_response(handle.result()))
        tracking.exclude_conversation(CONVERSATION_ID)
        self.assertEqual(handle.attempt.checked_state().code, 'conversation_unresolved')
        handle.withdraw()
        self.assertEqual(handle.attempt.checked_state().code, 'request_stopped')
        self.now=115.0
        self.assertEqual(handle.attempt.checked_state().code, 'request_timeout')
        self.assertTrue(handle.attempt.snapshot().verified)
        self.assertEqual(self.remaining(Allowance.WRITE), 9)

    def test_uncertain_preference_does_not_block_unrelated_creation_after_completion(self):
        tracking=self.setup_tracking()
        def failed():raise RuntimeError('uncertain storage write')
        self.storage.hooks['write_preference']=failed
        self.assertEqual(self.update(tracking).result().status,'unresolved')
        self.assertTrue(tracking.preference_unresolved(USER_ID))
        self.assertEqual(self.create(tracking).result().status,'ready')
        self.assertTrue(tracking.preference_unresolved(USER_ID))

    def test_final_check_resamples_time_after_waiting_for_tracking_guard(self):
        from concurrent.futures import ThreadPoolExecutor
        from tests.test_admission import ObservedLock
        for operation in ['create','list']:
            with self.subTest(operation=operation):
                self.now = 100.0
                tracking = self.setup_tracking()
                handle=self.create(tracking) if operation=='create' else self.listing(tracking)
                if operation=='create':
                    handle.attempt.bind_response(api.project_creation_response(handle.result()))
                native = threading.Lock()
                entered = threading.Event()
                tracking._lock=ObservedLock(native,entered.set)
                native.acquire()
                with ThreadPoolExecutor(1) as control:
                    task=control.submit(handle.attempt.checked_state)
                    try:
                        self.assertTrue(entered.wait(2))
                        self.now = 115.0
                    finally:
                        native.release()
                    self.assertEqual(task.result(2).code,'request_timeout')

    def test_failed_exclusion_check_maps_to_technical_error_not_success(self):
        tracking = self.setup_tracking()
        handle = self.create(tracking)
        handle.attempt.bind_response(api.project_creation_response(handle.result()))
        class BrokenGuard:
            def __enter__(self):raise RuntimeError('private control failure')
            def __exit__(self,*details):return False
        tracking._lock=BrokenGuard()
        try:
            code = handle.attempt.checked_state().code
        except RuntimeError:
            code = "escaped_control_error"
        self.assertEqual(code,'technical_error')

    def test_uncertain_write_allowance_never_enters_either_writer_or_retries(self):
        from unittest.mock import patch
        tracking=self.setup_tracking()
        for operation in ['create','update']:
            calls=[]

            def failed(category):
                calls.append(category)
                raise RuntimeError("PRIVATE quota fault")

            with patch.object(self.allowances,'_try_commit_locked',failed):
                result=(self.create(tracking) if operation=='create' else self.update(tracking)).result()
            self.assertEqual(result.status, "unresolved")
            self.assertIsNone(result.charged)
            self.assertEqual(calls,[Allowance.WRITE])
        self.assertNotIn('write_header',[r[0] for r in self.storage.calls])
        self.assertNotIn('write_preference',[r[0] for r in self.storage.calls])

    def test_write_authorization_resamples_deadline_after_tracking_guard_wait(self):
        from tests.test_admission import ObservedLock
        for operation in ['create','update']:
            with self.subTest(operation=operation):
                self.now = 100.0
                self.storage.calls = []
                tracking = self.setup_tracking()
                native = threading.Lock()
                entered = threading.Event()
                locked = []
                name='retain' if operation=='create' else 'retain_preference'
                retain=getattr(tracking,name)
                def delayed(intent):
                    retain(intent)
                    native.acquire()
                    locked.append(True)
                    tracking._lock=ObservedLock(native,entered.set)
                setattr(tracking,name,delayed)
                executor=ObservedThreadPool(max_workers=1)
                self.addCleanup(executor.shutdown,wait=True,cancel_futures=True)
                handle=self.create(tracking,executor=executor) if operation=='create' else self.update(tracking,executor=executor)
                try:
                    self.assertTrue(entered.wait(2))
                    self.now = 115.0
                finally:
                    if locked:
                        native.release()
                self.assertEqual(handle.result(2).status,'stopped')
                self.assertNotIn('write_header' if operation=='create' else 'write_preference',
                                 [row[0] for row in self.storage.calls])
