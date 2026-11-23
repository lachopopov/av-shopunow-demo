# Python 3.12; controlled storage exercises the real approved admission boundary.
import threading
import unittest
from uuid import UUID

from limits.storage import MemoryStorage
from pydantic import ValidationError
from shopunow import admission
from shopunow.allowances import Allowance, RollingAllowances
from shopunow.schemas import QuestionRequest
from tests.test_admission import InlineExecutor
from tests.test_owned_operations import OwnedStorage, NOW, RID
from tests.test_question_admission import SimulatedReaders
from tests.test_records import USER_ID, CONVERSATION_ID


class FoundationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.native = MemoryStorage()
        self.addCleanup(self.native.reset)
        self.allowances = RollingAllowances(self.native, threading.Lock())
        self.slots = admission.ExecutionSlots()
        self.reservations = admission.ResourceReservations()
        self.tracking = admission.OwnedRecordTracking()
        self.readers = SimulatedReaders()

    def create(self):
        storage = OwnedStorage()
        storage.tracking = self.tracking
        result = admission.submit_conversation_creation(user_id=USER_ID, request_id=RID,
            slots=self.slots, reservations=self.reservations, allowances=self.allowances,
            executor=InlineExecutor(), storage=storage, supervisor=self.tracking,
            tracking=self.tracking, clock=lambda:100., utc_now=lambda:NOW,
            uuid_factory=lambda:UUID(CONVERSATION_ID)).result()
        self.assertEqual(result.status, 'ready')

    def question(self, tracking=None, **fields):
        return admission.submit_question_admission(
            {'user_id':USER_ID, 'conversation_id':CONVERSATION_ID, 'question':'Question?', **fields},
            slots=self.slots, reservations=self.reservations, allowances=self.allowances,
            executor=InlineExecutor(), readers=self.readers, clock=lambda:100.,
            tracking=self.tracking if tracking is None else tracking).result()

    def test_stored_header_without_verified_origin_is_read_only_and_uncharged(self):
        result = self.question()
        self.assertEqual(result.status, 'conversation_read_only')
        self.assertIs(result.charged, False)
        self.assertEqual([call[0] for call in self.readers.calls], ['profile','header'])
        self.assertEqual(sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(11)), 10)

    def test_verified_origin_survives_new_requests_but_not_worker_replacement(self):
        self.create()
        self.assertEqual(self.question().status, 'admitted')
        self.assertEqual(self.question().status, 'admitted')  # Browser refresh is not a backend restart.
        self.assertEqual(self.question(tracking=admission.OwnedRecordTracking()).status,
                         'conversation_read_only')

    def test_style_enum_is_accepted_captured_and_does_not_change_stored_data(self):
        self.create()
        for style in ('brief','detailed',None):
            try:
                result = self.question(response_style=style)
            except ValidationError:
                self.fail('The approved session style is rejected by QuestionRequest.')
            self.assertEqual(result.status, 'admitted')
            self.assertEqual(getattr(result, 'response_style', 'missing'), style)
        self.assertIsNone(getattr(self.question(), 'response_style', 'missing'))
        for bad in ('other', 1, {'style':'brief'}):
            with self.assertRaises(ValidationError):
                QuestionRequest(user_id=USER_ID, conversation_id=CONVERSATION_ID,
                                question='Question?', response_style=bad)

    def test_known_uncertainty_wins_over_missing_origin(self):
        self.tracking.exclude_conversation(CONVERSATION_ID)
        self.assertEqual(self.question().status, 'conversation_unresolved')

    def test_mutating_request_style_during_a_read_cannot_change_captured_selection(self):
        from tests.test_admission import ObservedThreadPool
        self.create()
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def block():
            entered.set()
            if not release.wait(3):
                raise AssertionError('Profile barrier was not released.')
        self.readers.hooks['profile']=block
        request={'user_id':USER_ID,'conversation_id':CONVERSATION_ID,
                 'question':'Question?','response_style':'brief'}
        with ObservedThreadPool(max_workers=1) as pool:
            handle=admission.submit_question_admission(request,slots=self.slots,
                reservations=self.reservations,allowances=self.allowances,executor=pool,
                readers=self.readers,clock=lambda:100.,tracking=self.tracking)
            try:
                self.assertTrue(entered.wait(2))
                request['response_style']='detailed'
            finally:
                release.set()
            result=handle.result(3)
        self.assertEqual((result.status,result.response_style),('admitted','brief'))

    def test_supported_server_is_one_worker_without_reload(self):
        try:
            from shopunow.server import server_config
        except ImportError:
            self.fail('The explicit supported one-worker server configuration is missing.')
        from shopunow.api import create_app
        config = server_config(create_app(),port=0)
        self.assertEqual(config.workers,1)
        self.assertFalse(config.reload)
        self.assertEqual(config.host,'127.0.0.1')
        self.assertTrue(callable(config.app))
