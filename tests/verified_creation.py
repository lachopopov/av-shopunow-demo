# Python 3.12; test-only setup through real creation and exact read-back, no grants by fiat.
from threading import Lock
from uuid import UUID
from limits.storage import MemoryStorage
from shopunow import admission
from shopunow.allowances import RollingAllowances
from tests.test_admission import InlineExecutor
from tests.test_owned_operations import OwnedStorage, NOW, RID
from tests.test_records import USER_ID, CONVERSATION_ID


def verified_tracking(tracking=None, *, conversation_id=CONVERSATION_ID):
    tracking = admission.OwnedRecordTracking() if tracking is None else tracking
    storage = OwnedStorage()
    storage.tracking = tracking
    native = MemoryStorage()
    try:
        result = admission.submit_conversation_creation(user_id=USER_ID, request_id=RID,
            slots=admission.ExecutionSlots(), reservations=admission.ResourceReservations(),
            allowances=RollingAllowances(native, Lock()), executor=InlineExecutor(),
            storage=storage, supervisor=tracking, tracking=tracking, clock=lambda:100.,
            utc_now=lambda:NOW, uuid_factory=lambda:UUID(conversation_id)).result()
        if result.status != 'ready':
            raise AssertionError('Fixture creation did not pass exact verification.')
        return tracking
    finally:
        native.reset()  # Separate setup allowances never consume the assertion's request windows.
