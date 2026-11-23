# Python 3.12 / limits 5.8.0; simulated storage, actual verification and allowance logic.
import json
import threading
import unittest
from copy import deepcopy
from unittest.mock import patch
from limits.storage import MemoryStorage
from shopunow import admission, api
from shopunow.allowances import Allowance, RollingAllowances
from tests.test_admission import InlineExecutor
from tests.test_records import USER_ID, profile_result

REQUEST_ID = '01234567-89ab-4cde-8fab-0123456789ab'
EMPTY = {'ids': [], 'metadatas': [], 'documents': [], 'embeddings': None,
         'uris': None, 'data': None, 'included': ['metadatas', 'documents']}


def preference_result(value='detailed'):
    # Independently hand-defined exact-read shape, including all record fields.
    return {'ids': ['preference:syn-cust-01:response_style'],
            'metadatas': [{'record_type': 'preference', 'owner_user_id': USER_ID,
                           'key': 'response_style'}],
            'documents': [json.dumps({'record_type': 'preference', 'owner_user_id': USER_ID,
                'key': 'response_style', 'value': value, 'version': 2, 'complete': True,
                'updated_at': '2026-11-02T10:00:00Z'})],
            'embeddings': None, 'uris': None, 'data': None,
            'included': ['metadatas', 'documents']}


class IdentityReaders:
    def __init__(self):
        self.profile_data = profile_result()
        self.preference_data = deepcopy(EMPTY)
        self.calls = []
        self.hooks = {}

    def profile(self, user_id, *, timeout):
        self.calls.append(('profile', user_id, timeout))
        if 'profile' in self.hooks:
            self.hooks['profile']()
        return self.profile_data

    def preference(self, user_id, *, timeout):
        self.calls.append(('preference', user_id, timeout))
        if 'preference' in self.hooks:
            self.hooks['preference']()
        return self.preference_data


class IdentityReadTests(unittest.TestCase):
    def setUp(self):
        self.slots = admission.ExecutionSlots()
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.allowances = RollingAllowances(self.storage, threading.Lock())
        self.readers = IdentityReaders()
        self.now = 100.0

    def submit(self, operation='profile', **options):
        self.assertTrue(callable(getattr(admission, 'submit_identity_read', None)),
                        'Missing complete identity-read coordinator')
        return admission.submit_identity_read(
            {'request_id': REQUEST_ID, 'user_id': USER_ID}, operation=operation,
            slots=self.slots, allowances=self.allowances,
            executor=options.pop('executor', InlineExecutor()), readers=self.readers,
            roster=frozenset({USER_ID}), projector=options.pop('projector', api.project_identity_read),
            clock=lambda: self.now, **options)

    def remaining(self):
        return sum(self.allowances.try_commit(Allowance.PROFILE_READ) for _ in range(31))

    def test_profile_projects_only_stored_binding_and_counts_once(self):
        result = self.submit().result()
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.response.model_dump(), {
            'request_id': REQUEST_ID, 'user_id': USER_ID, 'created': False,
            'profile': {'user_id': USER_ID, 'role': 'customer', 'home_department': None}})
        self.assertEqual(self.remaining(), 29)
        self.assertEqual([row[0] for row in self.readers.calls], ['profile'])

    def test_preference_absence_is_null_and_counts(self):
        result = self.submit('preference').result()
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.response.model_dump(), {
            'request_id': REQUEST_ID, 'user_id': USER_ID, 'response_style': None})
        self.assertEqual(self.remaining(), 29)

    def test_explicit_preference_is_verified_and_projected(self):
        self.readers.preference_data = preference_result()
        result = self.submit('preference').result()
        self.assertEqual(result.response.response_style, 'detailed')
        self.assertEqual([row[:2] for row in self.readers.calls],
                         [('profile', USER_ID), ('preference', USER_ID)])
        self.assertTrue(all(0 < row[2] <= 8 for row in self.readers.calls))
        self.assertEqual(self.remaining(), 29)

    def test_unavailable_profile_stops_before_preference_and_hit(self):
        self.readers.profile_data = deepcopy(EMPTY)
        result = self.submit('preference').result()
        self.assertEqual(result.status, 'profile_unavailable')
        self.assertIsNone(result.response)
        self.assertEqual(len(self.readers.calls), 1)
        self.assertEqual(self.remaining(), 30)

    def test_corrupt_or_foreign_records_never_charge_or_project(self):
        for target in ['profile', 'preference']:
            for fault in ['foreign', 'incomplete', 'extra', 'malformed']:
                with self.subTest(target=target, fault=fault):
                    self.readers = IdentityReaders()
                    value = profile_result() if target == 'profile' else preference_result()
                    doc = json.loads(value['documents'][0])
                    if fault == 'foreign':
                        doc['user_id' if target == 'profile' else 'owner_user_id'] = 'syn-cust-02'
                    elif fault == 'incomplete':
                        doc['complete'] = False
                    elif fault == 'extra':
                        doc['private_content'] = 'must never escape'
                    value['documents'][0] = '{' if fault == 'malformed' else json.dumps(doc)
                    setattr(self.readers, target+'_data', value)
                    result = self.submit('preference').result()
                    self.assertEqual(result.status, 'unresolved')
                    self.assertIsNone(result.response)
        self.assertEqual(self.remaining(), 30)

    def test_both_operations_share_one_window_not_history(self):
        for _ in range(15):
            self.assertEqual(self.submit().result().status, 'ready')
            self.assertEqual(self.submit('preference').result().status, 'ready')
        self.assertEqual(self.submit().result().status, 'quota_exhausted')
        self.assertTrue(self.allowances.try_commit(Allowance.HISTORY))
        self.assertTrue(self.allowances.try_commit(Allowance.WRITE))

    def test_projection_failure_or_selector_rebinding_precedes_hit(self):
        for failure in ['raise', 'selector', 'constructed']:
            with self.subTest(failure=failure):
                def broken(*args, **kwargs):
                    if failure == 'raise':
                        raise ValueError('private-content')
                    value = api.project_identity_read(*args, **kwargs)
                    object.__setattr__(value, 'user_id', 'syn-cust-02' if failure == 'selector' else 12)
                    return value
                result = self.submit(projector=broken).result()
                self.assertEqual(result.status, 'unresolved')
                self.assertIsNone(result.response)
        self.assertEqual(self.remaining(), 30)

    def test_expiry_before_hit_consumes_nothing(self):
        self.readers.hooks['profile'] = lambda: setattr(self, 'now', 115.0)
        result = self.submit().result()
        self.assertEqual(result.status, 'stopped')
        self.assertFalse(result.charged)
        self.assertEqual(self.remaining(), 30)

    def test_late_hit_remains_charged_without_success(self):
        original = self.allowances._try_commit_locked
        def late(category):
            answer = original(category)
            self.now = 115.0
            return answer
        with patch.object(self.allowances, '_try_commit_locked', late):
            result = self.submit().result()
        self.assertEqual(result.status, 'stopped')
        self.assertTrue(result.charged)
        self.assertIsNone(result.response)
        self.assertEqual(self.remaining(), 29)

    def test_hit_exception_is_uncertain_and_not_retried(self):
        calls = []
        def uncertain(category):
            calls.append(category)
            raise RuntimeError('private-content')
        with patch.object(self.allowances, '_try_commit_locked', uncertain):
            result = self.submit().result()
        self.assertEqual(result.status, 'unresolved')
        self.assertIsNone(result.charged)
        self.assertEqual(calls, [Allowance.PROFILE_READ])
        self.assertIsNone(result.response)

    def test_valid_but_wrong_projection_content_is_not_admitted(self):
        # A syntactically valid projection must still describe the verified source values.
        for operation in ['profile', 'preference']:
            with self.subTest(operation=operation):
                def changed(*args, **kwargs):
                    value = api.project_identity_read(*args, **kwargs)
                    if operation == 'profile':
                        object.__setattr__(value.profile, 'role', 'employee')
                        object.__setattr__(value.profile, 'home_department', 'hr')
                    else:
                        object.__setattr__(value, 'response_style', 'brief')
                    return value
                result = self.submit(operation, projector=changed).result()
                self.assertEqual(result.status, 'unresolved')
                self.assertIsNone(result.response)
        self.assertEqual(self.remaining(), 30)

    def test_completed_result_rechecks_deadline_and_withdrawal(self):
        for reason in ['expired', 'withdrawn']:
            with self.subTest(reason=reason):
                self.now = 100.0
                handle = self.submit()
                self.assertEqual(handle.result().status, 'ready')
                if reason == 'expired':
                    self.now = 115.0
                else:
                    handle.withdraw()
                result = handle.result()
                self.assertEqual(result.status, 'stopped')
                self.assertIsNone(result.response)
                self.assertTrue(result.charged)
        self.assertEqual(self.remaining(), 28)
