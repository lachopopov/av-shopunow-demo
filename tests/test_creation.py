# Python v3.12 / Pydantic v2.14.1 / limits v5.8.0.
# Controlled write boundaries exercise actual local coordination, never Cloud storage.
import json  # Build native-shaped read-back independently of production verification.
import threading  # Establish running-work overlap without sleeps.
import unittest  # Use the existing offline runner.
import warnings  # Detect content-bearing serialization diagnostics.
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID

from limits.storage import MemoryStorage

from shopunow import admission, records
from shopunow.allowances import Allowance, RollingAllowances
from tests.test_admission import InlineExecutor, ObservedThreadPool, SubmitThenRaise
from tests.test_records import USER_ID, CONVERSATION_ID, HEADER, header_result, profile_result

REFERENCE = "ffffffff-ffff-4fff-8fff-ffffffffffff"
CREATED = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)


class LocalSupervisor:
    """Test-only owner: retain intents beyond worker completion; never clear on a timer."""

    def __init__(self):
        self.intents = {}  # Strong references survive the submitting handle's lifetime.
        self.lock = threading.Lock()
        self.fail = False

    def retain(self, intent):
        with self.lock:
            if self.fail or intent.header.conversation_id in self.intents:
                raise RuntimeError("private retention failure")
            self.intents[intent.header.conversation_id] = intent

    def unresolved(self, conversation_id):
        with self.lock:
            intent = self.intents[UUID(conversation_id)]
        return intent.attempt.snapshot().unresolved_write

    def expose(self, conversation_id):
        if not self.intents[UUID(conversation_id)].attempt.snapshot().verified:
            raise ValueError("Unresolved local ID")  # No simulated exposure or reuse.
        return conversation_id


class LocalStorage:
    """A synchronous boundary owns every simulated descendant and cleanup until return."""

    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.calls = []
        self.hooks = {}
        self.profile_value = profile_result()
        self.readback = header_result()
        self.saved = None

    def observe(self, kind, timeout):
        self.calls.append((kind, timeout))
        if kind in self.hooks:
            self.hooks[kind]()

    def profile(self, user_id, *, timeout):
        self.observe("profile", timeout)
        return self.profile_value

    def write_header(self, header, *, timeout):
        # The writer asserts observable retained state BEFORE even entering its hook.
        intent = self.supervisor.intents[header.conversation_id]
        if intent.header != header or not intent.attempt.snapshot().unresolved_write:
            raise AssertionError("Writer ran without retained conservative intent")
        self.saved = header
        self.observe("write", timeout)
        return None  # A native method acknowledgement is deliberately not proof.

    def header(self, conversation_id, *, timeout):
        self.observe("header", timeout)
        return self.readback


class IntendedHeaderTests(unittest.TestCase):
    def verify(self, native=None, expected=None):
        self.assertTrue(callable(getattr(records, "verify_created_conversation_header", None)),
                        "Intended-header verifier is missing")
        return records.verify_created_conversation_header(
            header_result() if native is None else native,
            expected=records.ConversationHeader.model_validate_json(json.dumps(HEADER))
            if expected is None else expected,
        )

    def test_matching_complete_header_and_equivalent_timestamp_instant(self):
        for timestamp in [HEADER["created_at"], "2026-11-02T11:00:00+01:00"]:
            checked = self.verify(header_result({**HEADER, "created_at": timestamp}))
            self.assertEqual(checked.status, "verified")
            self.assertEqual(checked.record.created_at, CREATED)

    def test_changed_fields_and_bad_native_shapes_never_verify(self):
        bad = [header_result({**HEADER, key: value}) for key, value in [
            ("created_at", "2026-11-02T10:00:01Z"), ("owner_user_id", "other-user"),
            ("conversation_id", REFERENCE), ("record_type", "profile"), ("complete", False),
        ]]
        bad += [{}, {"ids": [], "metadatas": [], "documents": []},
                {key: value * 2 if isinstance(value, list) else value
                 for key, value in header_result().items()}]
        mismatch = header_result()
        mismatch["metadatas"][0]["owner_user_id"] = "other-user"
        bad.append(mismatch)
        for native in bad:
            with self.subTest(native=native):
                checked = self.verify(native)
                self.assertNotEqual(checked.status, "verified")
                self.assertIsNone(checked.record)

    def test_constructed_expected_model_is_revalidated_without_content_warning(self):
        expected = records.ConversationHeader.model_construct(**{
            **HEADER, "created_at": ["PRIVATE malformed time"],
        })
        with warnings.catch_warnings(record=True) as captured:
            with self.assertRaises((ValueError, TypeError)):
                self.verify(expected=expected)
        self.assertEqual(captured, [])


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.slots = admission.ExecutionSlots()
        self.reservations = admission.ResourceReservations()
        self.memory = MemoryStorage()
        self.addCleanup(self.memory.reset)
        self.allowances = RollingAllowances(self.memory, threading.Lock())
        self.supervisor = LocalSupervisor()
        self.storage = LocalStorage(self.supervisor)
        self.now = 100.0
        self.executor = ObservedThreadPool(max_workers=8)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)

    def submit(self, **options):
        self.assertTrue(callable(getattr(admission, "submit_conversation_creation", None)),
                        "Local creation coordinator is missing")
        defaults = dict(
            user_id=USER_ID, request_id=REFERENCE, slots=self.slots,
            reservations=self.reservations, allowances=self.allowances,
            executor=InlineExecutor(), storage=self.storage, supervisor=self.supervisor,
            clock=lambda: self.now, uuid_factory=lambda: UUID(CONVERSATION_ID), utc_now=lambda: CREATED,
        )
        defaults.update(options)
        return admission.submit_conversation_creation(**defaults)

    def remaining(self):
        return sum(self.allowances.try_commit(Allowance.WRITE) for _ in range(11))

    def assert_result(self, handle, status, charged=False):
        result = handle.result(2)
        self.assertEqual(result.status, status)
        self.assertIs(result.charged, charged)
        self.assertEqual(result.conversation_id is not None, status == "ready")
        return result

    def block(self, kind):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def wait():
            entered.set()
            if not release.wait(3):
                raise AssertionError("Controlled work not released")
        self.storage.hooks[kind] = wait
        return entered, release

    def test_success_verifies_before_return_and_uses_write_allowance_only(self):
        handle = self.submit()
        result = self.assert_result(handle, "ready", True)
        self.assertEqual((result.request_id, result.user_id, result.conversation_id),
                         (REFERENCE, USER_ID, CONVERSATION_ID))
        self.assertEqual([call[0] for call in self.storage.calls], ["profile", "write", "header"])
        self.assertFalse(self.supervisor.unresolved(CONVERSATION_ID))
        self.assertEqual(self.remaining(), 9)
        self.assertEqual(sum(self.allowances.try_commit(Allowance.PROFILE_READ) for _ in range(31)), 30)

    def test_completed_success_is_not_reissued_after_deadline_or_withdrawal(self):
        for stopped in ["deadline", "withdrawal"]:
            with self.subTest(stopped=stopped):
                self.now = 100.0
                self.supervisor = LocalSupervisor()
                self.storage = LocalStorage(self.supervisor)
                handle = self.submit()
                if stopped == "deadline":
                    self.now = 115.0
                else:
                    handle.withdraw()
                self.assert_result(handle, "stopped", True)
                self.assertFalse(self.supervisor.unresolved(CONVERSATION_ID))

    def test_retention_failure_causes_no_write_but_keeps_committed_charge(self):
        self.supervisor.fail = True
        self.assert_result(self.submit(), "unresolved", True)
        self.assertEqual([c[0] for c in self.storage.calls], ["profile"])
        self.assertEqual(self.remaining(), 9)

    def test_prewrite_state_failure_causes_no_writer_call(self):
        self.assertTrue(hasattr(admission, "CreationAttempt"), "Creation attempt is missing")
        with patch.object(admission.CreationAttempt, "authorize_write", side_effect=RuntimeError("PRIVATE")):
            self.assert_result(self.submit(), "unresolved", True)
        self.assertEqual([c[0] for c in self.storage.calls], ["profile"])
        self.assertEqual(len(self.supervisor.intents), 1)

    def test_ack_followed_by_absent_malformed_or_changed_readback_stays_unresolved(self):
        for native in [{}, {"ids": [], "metadatas": [], "documents": []},
                       header_result({**HEADER, "created_at": "2026-11-03T10:00:00Z"})]:
            with self.subTest(native=native):
                self.supervisor = LocalSupervisor()
                self.storage = LocalStorage(self.supervisor)
                self.storage.readback = native
                self.assert_result(self.submit(), "unresolved", True)
                self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))
                with self.assertRaises(ValueError):
                    self.supervisor.expose(CONVERSATION_ID)

    def test_saved_then_raised_write_survives_completion_without_retry(self):
        def fail():
            raise RuntimeError("PRIVATE simulated transport failure")
        self.storage.hooks["write"] = fail
        self.assert_result(self.submit(), "unresolved", True)
        self.assertIsNotNone(self.storage.saved)
        self.assertEqual([c[0] for c in self.storage.calls], ["profile", "write"])
        self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))
        self.assert_result(self.submit(), "unresolved", True)  # Retention rejects ID reuse.
        self.assertEqual(sum(c[0] == "write" for c in self.storage.calls), 1)

    def test_invalid_request_and_profile_reject_before_allowance_or_write(self):
        for options in [dict(user_id="../bad"), dict(request_id="not-uuid")]:
            with self.assertRaises(ValueError):
                self.submit(**options)
        self.assertEqual(self.storage.calls, [])
        self.storage.profile_value = {"ids": [], "metadatas": [], "documents": []}
        self.assert_result(self.submit(), "profile_unavailable")
        self.assertEqual(self.remaining(), 10)

    def test_exhausted_write_allowance_has_no_intent_or_write(self):
        self.assertEqual(self.remaining(), 10)
        self.assert_result(self.submit(), "quota_exhausted")
        self.assertEqual(self.supervisor.intents, {})
        self.assertEqual([c[0] for c in self.storage.calls], ["profile"])

    def test_late_or_unknown_allowance_hit_never_starts_writer(self):
        for committed in [True, False, RuntimeError("PRIVATE")]:
            with self.subTest(committed=committed):
                self.now = 100.0
                def hit(category):
                    self.now = 115.0
                    if isinstance(committed, Exception):
                        raise committed
                    return committed
                with patch.object(self.allowances, "_try_commit_locked", side_effect=hit):
                    self.assert_result(self.submit(), "stopped", True if committed is True else
                                       None if isinstance(committed, Exception) else False)
                self.assertIsNone(self.storage.saved)
        self.now = 100.0
        with patch.object(self.allowances, "_try_commit_locked", side_effect=RuntimeError("PRIVATE")) as hit:
            self.assert_result(self.submit(), "unresolved", None)
            self.assertEqual(hit.call_count, 1)

    def test_bounded_reads_and_stop_after_retention_prevent_write(self):
        self.storage.hooks["profile"] = lambda: setattr(self, "now", 110.0)
        retain = self.supervisor.retain
        def retain_then_expire(intent):
            retain(intent)
            self.now = 115.0
        self.supervisor.retain = retain_then_expire
        self.assert_result(self.submit(), "stopped", True)
        self.assertEqual(self.storage.calls, [("profile", 8.0)])
        self.assertIsNone(self.storage.saved)

    def test_late_write_has_no_readback_or_success_and_retains_uncertainty(self):
        self.storage.hooks["write"] = lambda: setattr(self, "now", 115.0)
        self.assert_result(self.submit(), "stopped", True)
        self.assertEqual([c[0] for c in self.storage.calls], ["profile", "write"])
        self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))

    def test_cancellation_during_write_retains_capacity_user_key_and_unknown_intent(self):
        entered, release = self.block("write")
        owner = self.submit(executor=self.executor)
        self.assertTrue(entered.wait(2))
        self.assertFalse(owner.cancel())  # Running work cannot be cancelled by the caller.
        self.assert_result(self.submit(), "creation_busy")
        _holds = [self.slots.submit(self.executor, release.wait, 3) for _ in range(4)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit()
        release.set()
        self.assert_result(owner, "stopped", True)
        for dispatched in self.executor.dispatched:
            dispatched.result(2)  # Include completion-owned cleanup/release callbacks.
        self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))
        # The user key is released, but the supervisor still refuses this generated ID.
        self.assert_result(self.submit(), "unresolved", True)
        self.assertEqual(sum(c[0] == "write" for c in self.storage.calls), 1)

    def test_cancellation_during_profile_prevents_retention_and_write(self):
        entered, release = self.block("profile")
        owner = self.submit(executor=self.executor)
        self.assertTrue(entered.wait(2))
        owner.withdraw()
        release.set()
        self.assert_result(owner, "stopped")
        self.assertEqual(self.supervisor.intents, {})

    def test_late_readback_or_failed_readback_never_returns_id(self):
        for fail in [False, True]:
            self.supervisor = LocalSupervisor()
            self.storage = LocalStorage(self.supervisor)
            self.now = 100.0
            def read():
                if fail:
                    raise RuntimeError("PRIVATE read failure")
                self.now = 115.0
            self.storage.hooks["header"] = read
            self.assert_result(self.submit(), "unresolved" if fail else "stopped", True)
            self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))

    def test_creation_emits_no_payload_or_exception_diagnostics(self):
        def fail():
            raise RuntimeError("PRIVATE contact@example.test")
        self.storage.hooks["write"] = fail
        with self.assertNoLogs("shopunow.entry", level="DEBUG"):
            result = self.assert_result(self.submit(), "unresolved", True)
        self.assertNotIn("PRIVATE", repr(result))

    def test_retained_but_unwritten_intent_cannot_be_exposed(self):
        retain = self.supervisor.retain
        def expire(intent):
            retain(intent)
            self.now = 115.0
        self.supervisor.retain = expire
        self.assert_result(self.submit(), "stopped", True)
        with self.assertRaises(ValueError):
            self.supervisor.expose(CONVERSATION_ID)

    def test_write_budget_is_resampled_at_final_guarded_authorization(self):
        self.assertTrue(hasattr(admission, "CreationAttempt"), "Creation attempt is missing")
        authorize = admission.CreationAttempt.authorize_write
        def delayed(attempt):
            self.now = 112.0  # Model a control wait before the final guarded authorization.
            return authorize(attempt)
        with patch.object(admission.CreationAttempt, "authorize_write", delayed):
            self.assert_result(self.submit(), "ready", True)
        self.assertEqual(self.storage.calls, [("profile", 8.0), ("write", 3.0), ("header", 3.0)])

    def test_full_capacity_and_corrupt_profile_never_retain_or_write(self):
        hold = threading.Event()
        self.addCleanup(hold.set)
        _leases = [self.slots.submit(self.executor, hold.wait, 3) for _ in range(5)]
        with self.assertRaises(admission.AdmissionBusy):
            self.submit()
        self.assertEqual(self.storage.calls, [])
        hold.set()
        for dispatched in self.executor.dispatched:
            dispatched.result(2)
        self.storage.profile_value = {}
        self.assert_result(self.submit(), "unresolved")
        self.assertEqual(self.supervisor.intents, {})
        self.assertEqual(self.remaining(), 10)

    def test_confirmed_prestart_cancel_prevents_every_boundary_call(self):
        # Keep the native worker occupied so cancellation confirms prevention of start.
        with ObservedThreadPool(max_workers=1) as executor:
            hold, entered = threading.Event(), threading.Event()
            self.addCleanup(hold.set)
            executor.submit(lambda: (entered.set(), hold.wait(3)))
            self.assertTrue(entered.wait(2))
            handle = self.submit(executor=executor)
            try:
                self.assertTrue(handle.cancel())
            finally:
                hold.set()
        self.assertEqual(self.storage.calls, [])
        self.assertEqual(self.supervisor.intents, {})
        self.assertEqual(self.remaining(), 10)

    def test_dispatch_after_writer_started_retains_intent_and_user_reservation(self):
        entered, release = self.block("write")
        with self.assertRaises(RuntimeError):
            self.submit(executor=SubmitThenRaise(self.executor, started=entered))
        self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))
        self.assert_result(self.submit(), "creation_busy")
        release.set()
        for dispatched in self.executor.dispatched:
            dispatched.result(2)
        self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))
        self.assertEqual([c[0] for c in self.storage.calls].count("header"), 0)

    def test_visible_wait_timeout_retains_lease_and_supervisor_through_cleanup(self):
        entered, finish = threading.Event(), threading.Event()
        cleanup_entered, cleanup_finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        self.addCleanup(cleanup_finish.set)
        def work_and_cleanup():
            entered.set()
            try:
                if not finish.wait(3):
                    raise AssertionError("Write not released")
            finally:
                cleanup_entered.set()
                if not cleanup_finish.wait(3):
                    raise AssertionError("Cleanup not released")
        self.storage.hooks["write"] = work_and_cleanup
        handle = self.submit(executor=self.executor)
        self.assertTrue(entered.wait(2))
        from concurrent.futures import TimeoutError
        with self.assertRaises(TimeoutError):
            handle.result(0)  # Caller wait expiry records withdrawal, never worker completion.
        finish.set()
        self.assertTrue(cleanup_entered.wait(2))
        self.assert_result(self.submit(), "creation_busy")
        self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))
        cleanup_finish.set()
        self.assert_result(handle, "stopped", True)
        self.assertTrue(self.supervisor.unresolved(CONVERSATION_ID))

    def test_verification_finishing_after_deadline_never_returns_success(self):
        verify = records.verify_created_conversation_header
        def delayed(*args, **kwargs):
            checked = verify(*args, **kwargs)
            self.now = 115.0
            return checked
        with patch.object(admission, "verify_created_conversation_header", delayed):
            self.assert_result(self.submit(), "stopped", True)
        snapshot = self.supervisor.intents[UUID(CONVERSATION_ID)].attempt.snapshot()
        self.assertTrue(snapshot.verified)  # Known matching save, but no timely visible success.
        self.assertFalse(snapshot.unresolved_write)

    def test_writer_argument_cannot_mutate_the_retained_intended_header(self):
        write = self.storage.write_header
        def mutate(header, *, timeout):
            object.__setattr__(header, "created_at", datetime(2026, 11, 3, tzinfo=timezone.utc))
            return write(header, timeout=timeout)
        self.storage.write_header = mutate
        self.assert_result(self.submit(), "unresolved", True)
        self.assertEqual(self.supervisor.intents[UUID(CONVERSATION_ID)].header.created_at, CREATED)

    def test_withdrawal_while_waiting_for_allowance_guard_wins_without_charge(self):
        entered = threading.Event()
        from tests.test_admission import ObservedLock
        native = threading.Lock()
        self.allowances._commit_lock = ObservedLock(native, entered.set)
        native.acquire()
        try:
            handle = self.submit(executor=self.executor)
            self.assertTrue(entered.wait(2))
            handle.withdraw()
        finally:
            native.release()
        self.assert_result(handle, "stopped")
        self.assertIsNone(self.storage.saved)
        self.assertEqual(self.remaining(), 10)


# Flow: retained test intent -> controlled writer/read-back -> actual verification/state/leases.
