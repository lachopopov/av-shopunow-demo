# Python standard library v3.12; real threads verify admission and work lifetimes.
import asyncio  # Exercise asynchronous timeout and caller cancellation.
import gc  # Surface accidentally abandoned asynchronous failure notifications.
import threading  # Coordinate workers without sleep-based race assumptions.
import unittest  # Use the project's existing offline test runner.
from unittest.mock import patch  # Control only local limiter and recording boundaries.
from concurrent.futures import CancelledError, Future, InvalidStateError, ThreadPoolExecutor

from limits.storage import MemoryStorage  # Keep the approved native allowance real.

import shopunow.admission as admission_module  # Assert the additive gate's public behavior.
from shopunow.allowances import Allowance, RollingAllowances

from shopunow.admission import (
    AdmissionBusy, ExecutionLease, ExecutionSlots, ReservationConflict, ResourceReservations,
)


class BlockedWork:
    """Keep real work active until the test explicitly permits completion."""

    def __init__(self, *, failure=False, expected_starts=1):
        self.started = threading.Event()  # Prove actual callable entry.
        self.finish = threading.Event()  # Let cleanup unblock every owned thread.
        self.failure = failure
        self.expected_starts = expected_starts
        self.start_count = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.start_count += 1
            if self.start_count >= self.expected_starts:
                self.started.set()  # Signal only after every required worker entered.
        if not self.finish.wait(5):  # Bound a broken test rather than hang forever.
            raise TimeoutError("The test did not release its worker.")
        if self.failure:
            raise ValueError("controlled worker failure")
        return "finished"


class InlineExecutor:
    """A dispatch boundary whose real Future is complete before submit returns."""

    def submit(self, work, *args, **kwargs):
        completion = Future()
        completion.set_running_or_notify_cancel()
        try:
            completion.set_result(work(*args, **kwargs))
        except BaseException as error:
            completion.set_exception(error)
        return completion


class SubmitThenRaise:
    """Model a dispatcher that reports failure after accepting the callable."""

    def __init__(self, executor, *, started=None):
        self.executor = executor
        self.started = started

    def submit(self, work, *args, **kwargs):
        self.executor.submit(work, *args, **kwargs)
        if self.started is not None and not self.started.wait(3):
            raise AssertionError("The dispatched work did not start.")
        raise RuntimeError("dispatch outcome uncertain")


class ObservedThreadPool(ThreadPoolExecutor):
    """Expose native dispatch completion only to synchronize these thread tests."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dispatched = []

    def submit(self, work, /, *args, **kwargs):
        dispatched = super().submit(work, *args, **kwargs)
        self.dispatched.append(dispatched)
        return dispatched


class ObservedLock:
    """Observe attempted acquisition while preserving a native lock's exclusion."""

    def __init__(self, lock, before_enter):
        self.lock = lock
        self.before_enter = before_enter

    def __enter__(self):
        self.before_enter()  # Test scheduling observation happens before blocking.
        return self.lock.__enter__()

    def __exit__(self, *details):
        return self.lock.__exit__(*details)


class AdmissionTests(unittest.TestCase):
    """Each check catches missing admission or an incorrectly early slot release."""

    def setUp(self):
        self.slots = ExecutionSlots()  # One object is shared by every operation type.
        self.executor = ObservedThreadPool(max_workers=12)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)

    def blocked(self, *, failure=False, expected_starts=1):
        work = BlockedWork(failure=failure, expected_starts=expected_starts)
        self.addCleanup(work.finish.set)  # Runs before executor shutdown, even on RED.
        return work

    def fill(self, count=5):
        work = self.blocked(expected_starts=count)
        leases = [self.slots.submit(self.executor, work) for _ in range(count)]
        self.assertTrue(work.started.wait(3))
        return work, leases

    def assert_full(self, executor=None):
        called = threading.Event()
        with self.assertRaises(AdmissionBusy):
            self.slots.submit(executor or self.executor, called.set)
        self.assertFalse(called.is_set())  # Rejection must cause no callable effect.

    def test_five_running_operations_reject_sixth_without_running_it(self):
        self.fill()
        self.assert_full()

    def test_inline_worker_reserves_with_the_identical_returned_lease(self):
        # Creating the lease after dispatch would leave this inline worker without it.
        self.assertTrue(callable(getattr(self.slots, "submit_with_lease", None)))
        reservations = ResourceReservations()

        def reserve(lease, marker, *, executor, work, pass_lease, self):
            reservations.reserve(lease, ("conversation", "inline"))
            return lease, marker, executor, work, pass_lease, self

        lease = self.slots.submit_with_lease(
            InlineExecutor(), reserve, "positional", executor="executor keyword",
            work="work keyword", pass_lease="private keyword", self="self keyword",
        )
        result = lease.result(0)
        self.assertIs(result[0], lease)  # Reservation and caller share one completion owner.
        self.assertEqual(result[1:], (
            "positional", "executor keyword", "work keyword", "private keyword", "self keyword",
        ))
        # Inline completion must also release the worker's reservation for the next call.
        replacement = self.slots.submit_with_lease(
            InlineExecutor(), reserve, "next", executor=None, work=None,
            pass_lease=None, self=None,
        )
        self.assertIs(replacement.result(0)[0], replacement)

    def test_legacy_submit_preserves_positional_and_colliding_keyword_arguments(self):
        # Refactoring shared dispatch must not consume any keyword intended for work.
        def receive(marker, *, executor, work, pass_lease, self):
            return marker, executor, work, pass_lease, self

        lease = self.slots.submit(
            InlineExecutor(), receive, "positional", executor="executor keyword",
            work="work keyword", pass_lease="private keyword", self="self keyword",
        )
        self.assertEqual(lease.result(0), (
            "positional", "executor keyword", "work keyword", "private keyword", "self keyword",
        ))

    def test_both_submission_apis_share_five_slots(self):
        # A separate semaphore for the new API would admit a sixth active operation.
        self.assertTrue(callable(getattr(self.slots, "submit_with_lease", None)))
        self.fill(2)
        work = self.blocked(expected_starts=3)
        for _ in range(3):
            self.slots.submit_with_lease(self.executor, lambda lease: work())
        self.assertTrue(work.started.wait(3))
        self.assert_full()
        called = threading.Event()
        with self.assertRaises(AdmissionBusy):
            self.slots.submit_with_lease(self.executor, lambda lease: called.set())
        self.assertFalse(called.is_set())

    def test_lease_aware_confirmed_cancel_prevents_worker_invocation(self):
        self.assertTrue(callable(getattr(self.slots, "submit_with_lease", None)))
        serial = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(serial.shutdown, wait=True, cancel_futures=True)
        occupied = self.blocked()
        blocker = serial.submit(occupied)  # Occupy native dispatch outside admission.
        self.assertTrue(occupied.started.wait(3))
        self.fill(4)
        called = threading.Event()
        lease = self.slots.submit_with_lease(serial, lambda lease: called.set())
        self.assertTrue(lease.cancel())
        self.assertTrue(lease.cancel())  # Repeated cancellation cannot release twice.
        with self.assertRaises(CancelledError):
            lease.result(0)
        self.fill(1)
        self.assert_full()
        occupied.finish.set()
        blocker.result(3)
        serial.shutdown(wait=True)
        self.assertFalse(called.is_set())
        self.assert_full()  # A late cancelled wrapper cannot release the replacement's slot.

    def test_lease_aware_dispatch_error_holds_worker_reservation_through_cleanup(self):
        self.assertTrue(callable(getattr(self.slots, "submit_with_lease", None)))
        _, held_leases = self.fill(4)
        reservations = ResourceReservations()
        body_started = threading.Event()
        body_done = threading.Event()
        cleanup_started = threading.Event()
        cleanup_done = threading.Event()
        self.addCleanup(body_done.set)
        self.addCleanup(cleanup_done.set)
        key = ("conversation", "dispatch-error")

        def work_with_cleanup(lease):
            reservations.reserve(lease, key)  # The worker can claim before submit returns.
            body_started.set()
            try:
                if not body_done.wait(3):
                    raise TimeoutError("The test did not finish its operation body.")
                return 7
            finally:
                cleanup_started.set()
                if not cleanup_done.wait(3):
                    raise TimeoutError("The test did not finish its operation cleanup.")

        dispatcher = SubmitThenRaise(self.executor, started=body_started)
        with self.assertRaisesRegex(RuntimeError, "dispatch outcome uncertain"):
            self.slots.submit_with_lease(dispatcher, work_with_cleanup)
        dispatched = self.executor.dispatched[-1]
        self.assert_full()
        with self.assertRaises(ReservationConflict):
            reservations.reserve(held_leases[0], key)
        body_done.set()
        self.assertTrue(cleanup_started.wait(3))
        self.assert_full()
        with self.assertRaises(ReservationConflict):
            reservations.reserve(held_leases[0], key)
        cleanup_done.set()
        dispatched.result(3)  # Join completion callbacks before checking resource reuse.
        reservations.reserve(held_leases[0], key)
        self.assertEqual(self.slots.submit(InlineExecutor(), lambda: 7).result(0), 7)

    def test_simultaneous_submitters_cannot_oversubscribe_five_slots(self):
        # Removing atomic admission would admit more than five blocked operations.
        work = self.blocked()
        start = threading.Barrier(13)

        def attempt():
            start.wait(3)
            try:
                return self.slots.submit(self.executor, work)
            except AdmissionBusy:
                return None

        with ThreadPoolExecutor(max_workers=12) as callers:
            attempts = [callers.submit(attempt) for _ in range(12)]
            start.wait(3)
            results = [attempt.result(3) for attempt in attempts]
        self.assertEqual(sum(result is not None for result in results), 5)
        self.assertEqual(sum(result is None for result in results), 7)

    def test_completed_success_and_failure_restore_capacity(self):
        # Losing a completion callback leaks capacity after either terminal outcome.
        for failure in (False, True):
            with self.subTest(failure=failure):
                work, leases = self.fill(4)
                final = self.blocked(failure=failure)
                lease = self.slots.submit(self.executor, final)
                finished_dispatch = self.executor.dispatched[-1]
                self.assertTrue(final.started.wait(3))
                self.assert_full()
                final.finish.set()
                if failure:
                    with self.assertRaises(ValueError):
                        lease.result(3)
                else:
                    self.assertEqual(lease.result(3), "finished")
                finished_dispatch.result(3)  # Callable return includes its done callback.
                self.assertEqual(self.slots.submit(self.executor, lambda: 7).result(3), 7)
                work.finish.set()
                for active in leases:
                    active.result(3)
                for dispatched in self.executor.dispatched:
                    dispatched.result(3)  # Do not race the next subtest's admission.

    def test_dispatch_failure_racing_confirmed_cancel_has_no_callback_error(self):
        # Pending-check then set_exception must tolerate cancellation between them.
        failure_checked = threading.Event()
        finish_check = threading.Event()

        class PausedFailure(Future):
            def exception(self, timeout=None):
                failure_checked.set()  # Callback already observed a pending lease.
                if not finish_check.wait(3):
                    raise AssertionError("The test did not release failure reporting.")
                return super().exception(timeout)

        class FailedDispatch:
            future = PausedFailure()

            def submit(self, work):
                return self.future  # This controlled dispatch fails without starting work.

        dispatcher = FailedDispatch()
        lease = self.slots.submit(dispatcher, lambda: None)
        reporter = threading.Thread(
            target=dispatcher.future.set_exception, args=(RuntimeError("dispatch failed"),)
        )
        with self.assertNoLogs("concurrent.futures", level="ERROR"):
            reporter.start()
            try:
                self.assertTrue(failure_checked.wait(3))
                self.assertTrue(lease.cancel())
            finally:
                finish_check.set()
                reporter.join(3)
            self.assertFalse(reporter.is_alive())
        with self.assertRaises(CancelledError):
            lease.result(0)
        self.fill()
        self.assert_full()  # Failure reporting must not release cancelled capacity twice.

    def test_dispatch_rejection_restores_capacity(self):
        # A pre-dispatch failure must not leak an otherwise unused slot.
        self.fill(4)
        stopped = ThreadPoolExecutor(max_workers=1)
        stopped.shutdown()
        with self.assertRaises(RuntimeError):
            self.slots.submit(stopped, lambda: None)
        self.assertEqual(self.slots.submit(self.executor, lambda: 7).result(3), 7)

    def test_submit_error_after_start_keeps_slot_until_work_and_cleanup_end(self):
        held, _ = self.fill(4)
        work = self.blocked()
        dispatcher = SubmitThenRaise(self.executor, started=work.started)
        with self.assertRaises(RuntimeError):
            self.slots.submit(dispatcher, work)
        self.assert_full()
        work.finish.set()
        held.finish.set()  # Release the other four before joining this shared executor.
        self.executor.shutdown(wait=True)  # Join proves the uncertain work ended.
        self.assertEqual(self.slots.submit(InlineExecutor(), lambda: 7).result(3), 7)

    def test_submit_error_before_start_prevents_later_callable_execution(self):
        # A submit exception can leave a queued wrapper; cancellation must gate it.
        serial = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(serial.shutdown, wait=True, cancel_futures=True)
        occupied = self.blocked()
        blocker = serial.submit(occupied)
        self.assertTrue(occupied.started.wait(3))
        called = threading.Event()
        with self.assertRaises(RuntimeError):
            self.slots.submit(SubmitThenRaise(serial), called.set)
        occupied.finish.set()
        blocker.result(3)
        serial.shutdown(wait=True)
        self.assertFalse(called.is_set())
        self.fill()
        self.assert_full()  # The rejected dispatch's slot was restored exactly once.

    def test_visible_timeout_and_unconfirmed_cancel_keep_running_slot(self):
        work, leases = self.fill()
        with self.assertRaises(TimeoutError):
            leases[0].result(timeout=0)
        self.assertFalse(leases[0].cancel())
        self.assert_full()
        work.finish.set()
        for lease in leases:
            lease.result(3)
        for dispatched in self.executor.dispatched:
            dispatched.result(3)  # Wait for synchronous completion callbacks as well.
        self.assertEqual(self.slots.submit(self.executor, lambda: 7).result(3), 7)

    def test_confirmed_cancel_prevents_work_and_releases_exactly_once(self):
        serial = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(serial.shutdown, wait=True, cancel_futures=True)
        occupied = self.blocked()
        blocker = serial.submit(occupied)  # Deliberately occupy dispatch, not admission.
        self.assertTrue(occupied.started.wait(3))
        self.fill(4)
        called = threading.Event()
        lease = self.slots.submit(serial, called.set)
        self.assertTrue(lease.cancel())
        self.assertTrue(lease.cancel())  # Native repeated cancellation is idempotent.
        with self.assertRaises(CancelledError):
            lease.result(0)
        self.fill(1)
        self.assert_full()  # A double release would admit a sixth live operation.
        occupied.finish.set()
        blocker.result(3)
        serial.shutdown(wait=True)
        self.assertFalse(called.is_set())
        self.assert_full()  # The late cancelled wrapper must not release twice.

    def test_already_completed_dispatch_does_not_lose_or_duplicate_release(self):
        self.fill(4)
        inline = InlineExecutor()
        for _ in range(10):
            self.assertEqual(self.slots.submit(inline, lambda: 7).result(0), 7)
        self.fill(1)
        self.assert_full()

    def test_executor_shutdown_keeps_running_slots_and_cancels_unstarted_work(self):
        work, leases = self.fill()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.assert_full(InlineExecutor())
        work.finish.set()
        for lease in leases:
            lease.result(3)
        for dispatched in self.executor.dispatched:
            dispatched.result(3)  # Shutdown leaves callback ownership with each worker.
        self.assertEqual(self.slots.submit(InlineExecutor(), lambda: 7).result(0), 7)

        serial = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(serial.shutdown, wait=True, cancel_futures=True)
        occupied = self.blocked()
        serial.submit(occupied)
        self.assertTrue(occupied.started.wait(3))
        called = threading.Event()
        queued = self.slots.submit(serial, called.set)
        serial.shutdown(wait=False, cancel_futures=True)
        with self.assertRaises(CancelledError):
            queued.result(0)
        replacement = ThreadPoolExecutor(max_workers=5)
        self.addCleanup(replacement.shutdown, wait=True, cancel_futures=True)
        active = self.blocked(expected_starts=5)
        for _ in range(5):
            self.slots.submit(replacement, active)
        self.assertTrue(active.started.wait(3))
        self.assert_full(InlineExecutor())
        self.assertFalse(called.is_set())


class ReservationTests(unittest.TestCase):
    """Reserve real operation leases; caller waits never own resource release."""

    def setUp(self):
        self.slots = ExecutionSlots()
        self.reservations = ResourceReservations()  # One registry across all resource types.
        self.executor = ObservedThreadPool(max_workers=5)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)

    def operation(self, *, failure=False):
        work = BlockedWork(failure=failure)
        self.addCleanup(work.finish.set)  # Release before the executor cleanup joins.
        lease = self.slots.submit(self.executor, work)
        dispatched = self.executor.dispatched[-1]
        self.assertTrue(work.started.wait(3))
        return lease, work, dispatched

    def test_same_resource_rejects_another_active_operation(self):
        first, _, _ = self.operation()
        other, _, _ = self.operation()
        self.reservations.reserve(first, ("conversation", "one"))
        with self.assertRaises(ReservationConflict):
            self.reservations.reserve(other, ("conversation", "one"))

    def test_distinct_ids_and_resource_namespaces_do_not_conflict(self):
        first, _, _ = self.operation()
        other, _, _ = self.operation()
        # One operation may hold more than one key; these are not an atomic key group.
        for resource in (("conversation", "one"), ("user", "one"), ("offer", "one")):
            self.reservations.reserve(first, resource)
        self.reservations.reserve(other, ("conversation", "two"))
        for resource in (("conversation", "one"), ("user", "one"), ("offer", "one")):
            with self.subTest(resource=resource), self.assertRaises(ReservationConflict):
                self.reservations.reserve(other, resource)

    def test_simultaneous_operations_have_only_one_resource_winner(self):
        leases = [self.operation()[0] for _ in range(5)]
        start = threading.Barrier(6)

        def attempt(lease):
            start.wait(3)
            try:
                self.reservations.reserve(lease, ("conversation", "one"))
            except ReservationConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=5) as callers:
            attempts = [callers.submit(attempt, lease) for lease in leases]
            start.wait(3)
            outcomes = [attempt.result(3) for attempt in attempts]
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 4)

    def test_success_and_failure_release_only_the_finished_operations_keys(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                first, work, dispatched = self.operation(failure=failure)
                other, other_work, other_dispatch = self.operation()
                for resource in (("conversation", "one"), ("user", "one")):
                    self.reservations.reserve(first, resource)
                self.reservations.reserve(other, ("offer", "one"))
                work.finish.set()
                if failure:
                    with self.assertRaises(ValueError):
                        first.result(3)
                else:
                    self.assertEqual(first.result(3), "finished")
                dispatched.result(3)  # Native result wakeup alone need not await callbacks.
                for resource in (("conversation", "one"), ("user", "one")):
                    self.reservations.reserve(other, resource)
                replacement, replacement_work, replacement_dispatch = self.operation()
                self.assertFalse(first.cancel())  # A stale lease cannot release a new owner.
                for resource in (("conversation", "one"), ("offer", "one")):
                    with self.subTest(resource=resource), self.assertRaises(ReservationConflict):
                        self.reservations.reserve(replacement, resource)
                other_work.finish.set()
                replacement_work.finish.set()
                other_dispatch.result(3)
                replacement_dispatch.result(3)

    def test_timeout_and_uncancelled_work_keep_reservation_through_cleanup(self):
        body_started = threading.Event()
        body_done = threading.Event()
        cleanup_started = threading.Event()
        cleanup_done = threading.Event()
        self.addCleanup(body_done.set)
        self.addCleanup(cleanup_done.set)

        def work_with_cleanup():
            body_started.set()  # Only this operation's entry rules out pre-start cancellation.
            try:
                if not body_done.wait(3):
                    raise TimeoutError("The test did not finish its operation body.")
                return 7
            finally:
                cleanup_started.set()
                if not cleanup_done.wait(3):
                    raise TimeoutError("The test did not finish its operation cleanup.")

        first = self.slots.submit(self.executor, work_with_cleanup)
        dispatched = self.executor.dispatched[-1]
        self.assertTrue(body_started.wait(3))
        other, _, _ = self.operation()
        key = ("conversation", "one")
        self.reservations.reserve(first, key)
        with self.assertRaises(TimeoutError):
            first.result(0)
        self.assertFalse(first.cancel())
        with self.assertRaises(ReservationConflict):
            self.reservations.reserve(other, key)
        body_done.set()
        self.assertTrue(cleanup_started.wait(3))
        with self.assertRaises(ReservationConflict):
            self.reservations.reserve(other, key)
        cleanup_done.set()
        self.assertEqual(first.result(3), 7)
        dispatched.result(3)
        self.reservations.reserve(other, key)

    def test_confirmed_cancel_releases_without_late_wrapper_releasing_new_owner(self):
        serial = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(serial.shutdown, wait=True, cancel_futures=True)
        occupied = BlockedWork()
        self.addCleanup(occupied.finish.set)
        serial.submit(occupied)
        self.assertTrue(occupied.started.wait(3))
        called = threading.Event()
        queued = self.slots.submit(serial, called.set)
        other, _, _ = self.operation()
        key = ("offer", "one")
        self.reservations.reserve(queued, key)
        with self.assertRaises(ReservationConflict):
            self.reservations.reserve(other, key)
        self.assertTrue(queued.cancel())
        self.assertTrue(queued.cancel())
        self.reservations.reserve(other, key)
        with self.assertRaises(InvalidStateError):
            self.reservations.reserve(queued, ("offer", "two"))
        occupied.finish.set()
        serial.shutdown(wait=True)
        self.assertFalse(called.is_set())
        replacement, _, _ = self.operation()
        with self.assertRaises(ReservationConflict):
            self.reservations.reserve(replacement, key)

    def test_completed_lease_cannot_acquire_a_new_reservation(self):
        finished = self.slots.submit(InlineExecutor(), lambda: 7)
        with self.assertRaises(InvalidStateError):
            self.reservations.reserve(finished, ("user", "one"))

    def test_completion_during_callback_registration_does_not_deadlock_or_leak(self):
        class CompletesAtRegistration(Future):
            def add_done_callback(completion, callback):
                completion.set_result(7)  # Force the native immediate-callback path.
                return super().add_done_callback(callback)

        completion = CompletesAtRegistration()
        lease = ExecutionLease(completion)  # Native Future seam for this exact interleaving.
        errors = []

        def reserve():
            try:
                self.reservations.reserve(lease, ("user", "one"))
            except BaseException as error:
                errors.append(error)

        caller = threading.Thread(target=reserve, daemon=True)
        caller.start()
        caller.join(3)  # Bound a broken lock-around-callback implementation.
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(completion.done())
        replacement, _, _ = self.operation()
        self.reservations.reserve(replacement, ("user", "one"))
        other, _, _ = self.operation()
        with self.assertRaises(ReservationConflict):
            self.reservations.reserve(other, ("user", "one"))


class AsyncAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """Caller wait cancellation must never become a worker-completion signal."""

    async def test_timeout_and_cancelled_caller_retain_slot_and_observe_late_failure(self):
        for stop in ("timeout", "caller_cancel"):
            with self.subTest(stop=stop):
                slots = ExecutionSlots()
                reservations = ResourceReservations()
                executor = ThreadPoolExecutor(max_workers=5)
                work = BlockedWork(failure=True, expected_starts=5)
                errors = []
                loop = asyncio.get_running_loop()
                previous_handler = loop.get_exception_handler()
                loop.set_exception_handler(lambda loop, context: errors.append(context))
                try:
                    leases = [slots.submit(executor, work) for _ in range(5)]
                    self.assertTrue(work.started.wait(3))
                    reservations.reserve(leases[0], ("conversation", "one"))
                    if stop == "timeout":
                        with self.assertRaises(TimeoutError):
                            await leases[0].wait(timeout=0)
                    else:
                        caller = asyncio.create_task(leases[0].wait())
                        entered = asyncio.Event()
                        loop.call_soon(entered.set)  # Caller starts before this signal.
                        await entered.wait()
                        caller.cancel()  # Represents a request task ending/disconnecting.
                        with self.assertRaises(asyncio.CancelledError):
                            await caller
                    with self.assertRaises(ReservationConflict):
                        reservations.reserve(leases[1], ("conversation", "one"))
                    with self.assertRaises(AdmissionBusy):
                        slots.submit(executor, lambda: None)
                    work.finish.set()
                    for lease in leases:
                        with self.assertRaises(ValueError):
                            await lease.wait(timeout=3)
                    self.assertEqual(await slots.submit(executor, lambda: 7).wait(3), 7)
                    gc.collect()  # Abandoned shield/bridge exceptions must be consumed.
                    self.assertEqual(errors, [])
                finally:
                    work.finish.set()
                    executor.shutdown(wait=True, cancel_futures=True)
                    loop.set_exception_handler(previous_handler)

    async def test_cancelled_or_expired_wait_cannot_cancel_unstarted_work(self):
        # Without shield, cancelling an asyncio bridge can cancel queued native work.
        for stop in ("timeout", "caller_cancel"):
            with self.subTest(stop=stop):
                slots = ExecutionSlots()
                executor = ThreadPoolExecutor(max_workers=1)
                occupied = BlockedWork()
                try:
                    executor.submit(occupied)  # Occupy dispatch outside admission.
                    self.assertTrue(occupied.started.wait(3))
                    leases = [slots.submit(executor, lambda: 7) for _ in range(5)]
                    if stop == "timeout":
                        with self.assertRaises(TimeoutError):
                            await leases[0].wait(timeout=0)
                    else:
                        caller = asyncio.create_task(leases[0].wait())
                        entered = asyncio.Event()
                        asyncio.get_running_loop().call_soon(entered.set)
                        await entered.wait()
                        caller.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await caller
                    with self.assertRaises(AdmissionBusy):
                        slots.submit(InlineExecutor(), lambda: None)
                    occupied.finish.set()
                    for lease in leases:
                        self.assertEqual(await lease.wait(3), 7)
                finally:
                    occupied.finish.set()
                    executor.shutdown(wait=True, cancel_futures=True)


class QuestionAttemptTests(unittest.TestCase):
    """Catch incorrect deadline, charge, withdrawal, and work-permission decisions."""

    def setUp(self):
        self.now = 100.0  # Monotonic attempt time is independent of native wall time.
        self.storage = MemoryStorage()  # Retain the real moving-window hit and counts.
        self.addCleanup(self.stop_storage_timer)
        self.allowances = RollingAllowances(self.storage, threading.Lock())

    def stop_storage_timer(self):
        self.storage.timer.cancel()  # Fixture cleanup does not alter application lifecycle.
        self.storage.timer.join(3)

    def attempt(self):
        attempt_type = getattr(admission_module, "QuestionAttempt", None)
        self.assertTrue(callable(attempt_type), "The guarded question attempt is missing.")
        return attempt_type(100.0, clock=lambda: self.now)

    def assert_remaining_questions(self, count):
        # These are real commits, so an omitted/extra native charge changes the result.
        for _ in range(count):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.assertFalse(self.allowances.try_commit(Allowance.QUESTION))

    def test_deadline_equality_stops_before_charging_question_allowance(self):
        attempt = self.attempt()
        self.now = 115.0  # Equality is outside the provisional 15-second budget.
        self.assertEqual(attempt.try_admit(self.allowances), "stopped")
        self.assertFalse(attempt.charged)
        self.assertIsNone(attempt.admission)
        self.assertFalse(attempt.authorize_normal_work())
        self.assert_remaining_questions(10)

    def test_success_records_pre_hit_time_and_repeated_decision_charges_once(self):
        attempt = self.attempt()
        self.now = 114.0
        self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        self.assertTrue(attempt.charged)
        self.assertEqual(attempt.admission.admitted_at, 114.0)
        self.assertEqual(attempt.admission.question_deadline, 204.0)
        self.assertEqual(attempt.admission.department_cutoff, 174.0)
        self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        self.assertTrue(attempt.authorize_normal_work())
        self.assert_remaining_questions(9)

    def test_successful_hit_finishing_after_pre_deadline_remains_admitted_and_charged(self):
        attempt = self.attempt()
        native_hit = self.allowances._limiter.hit  # Preserve real quota insertion.

        def finish_late(*args, **kwargs):
            result = native_hit(*args, **kwargs)
            self.now = 116.0  # The hit started before 115; completion cannot refund it.
            return result

        self.now = 114.0
        with patch.object(self.allowances._limiter, "hit", side_effect=finish_late):
            self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        self.assertEqual(attempt.admission.admitted_at, 114.0)
        self.assertTrue(attempt.authorize_normal_work())
        self.assert_remaining_questions(9)

    def test_quota_rejection_is_final_and_does_not_charge_after_window_is_cleared(self):
        for _ in range(10):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        attempt = self.attempt()
        self.assertEqual(attempt.try_admit(self.allowances), "rejected")
        self.assertFalse(attempt.charged)
        self.assertFalse(attempt.authorize_normal_work())
        self.storage.reset()  # Native reset is fixture setup, never an application refund.
        self.assertEqual(attempt.try_admit(self.allowances), "rejected")
        self.assert_remaining_questions(10)

    def test_unadmitted_attempt_cannot_authorize_normal_work(self):
        self.assertFalse(self.attempt().authorize_normal_work())

    def test_question_deadline_equality_stops_authorization_without_refunding(self):
        attempt = self.attempt()
        self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        self.now = 189.999
        self.assertTrue(attempt.authorize_normal_work())
        self.now = 190.0
        self.assertFalse(attempt.authorize_normal_work())
        self.assertTrue(attempt.stopped)
        self.assert_remaining_questions(9)

    def test_department_cutoff_does_not_blanket_ban_later_approved_corrections(self):
        attempt = self.attempt()
        self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        self.now = 160.0  # Contributor cutoff is a later workflow responsibility.
        self.assertTrue(attempt.authorize_normal_work())
        self.now = 189.0
        self.assertTrue(attempt.authorize_normal_work())

    def test_hit_exhausting_question_budget_keeps_charge_without_work_permission(self):
        attempt = self.attempt()
        native_hit = self.allowances._limiter.hit

        def use_budget(*args, **kwargs):
            result = native_hit(*args, **kwargs)
            self.now = 190.0  # The pre-hit admission time remains 100.
            return result

        with patch.object(self.allowances._limiter, "hit", side_effect=use_budget):
            self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        self.assertFalse(attempt.authorize_normal_work())
        self.assertTrue(attempt.charged)
        self.assert_remaining_questions(9)

    def test_withdrawal_before_admission_is_final_and_consumes_no_allowance(self):
        attempt = self.attempt()
        attempt.withdraw()  # Recording withdrawal, rather than a physical disconnect, wins.
        self.assertEqual(attempt.try_admit(self.allowances), "stopped")
        self.assertEqual(attempt.try_admit(self.allowances), "stopped")
        self.assertFalse(attempt.authorize_normal_work())
        self.assert_remaining_questions(10)

    def test_withdrawal_after_commit_keeps_charge_and_prevents_new_permission(self):
        attempt = self.attempt()
        self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        attempt.withdraw()
        self.assertTrue(attempt.stopped)
        self.assertTrue(attempt.charged)
        self.assertFalse(attempt.authorize_normal_work())
        self.assertEqual(attempt.try_admit(self.allowances), "admitted")
        self.assert_remaining_questions(9)

    def test_raised_hit_is_unresolved_with_unknown_charge_and_no_retry(self):
        for charged_before_failure in (False, True):
            with self.subTest(charged_before_failure=charged_before_failure):
                self.storage.reset()  # Isolate the two actual native charge outcomes.
                attempt = self.attempt()
                native_hit = self.allowances._limiter.hit

                def uncertain_hit(*args, **kwargs):
                    if charged_before_failure:
                        native_hit(*args, **kwargs)  # A raised call may have inserted a hit.
                    raise RuntimeError("controlled uncertain hit")

                with patch.object(self.allowances._limiter, "hit", side_effect=uncertain_hit):
                    self.assertEqual(attempt.try_admit(self.allowances), "unresolved")
                self.assertIsNone(attempt.charged)  # An exception cannot prove no charge.
                self.assertTrue(attempt.stopped)
                self.assertFalse(attempt.authorize_normal_work())
                self.assertEqual(attempt.try_admit(self.allowances), "unresolved")
                self.assert_remaining_questions(9 if charged_before_failure else 10)

    def test_non_boolean_hit_is_unresolved_instead_of_rejected_or_admitted(self):
        for unknown_result in (None, 0, 1, "false"):
            with self.subTest(unknown_result=unknown_result):
                self.storage.reset()
                attempt = self.attempt()
                with patch.object(self.allowances._limiter, "hit", return_value=unknown_result):
                    self.assertEqual(attempt.try_admit(self.allowances), "unresolved")
                self.assertIsNone(attempt.charged)
                self.assertTrue(attempt.stopped)
                self.assertFalse(attempt.authorize_normal_work())
                self.assertEqual(attempt.try_admit(self.allowances), "unresolved")
                self.assert_remaining_questions(10)

    def test_failed_timing_record_keeps_known_charge_and_unresolved_admission(self):
        attempt = self.attempt()
        # Failure of the actual record-construction boundary needs no production hook.
        with patch.object(admission_module, "QuestionAdmission", side_effect=MemoryError):
            self.assertEqual(attempt.try_admit(self.allowances), "unresolved")
        self.assertTrue(attempt.charged)
        self.assertTrue(attempt.stopped)
        self.assertIsNone(attempt.admission)
        self.assertFalse(attempt.authorize_normal_work())
        self.assertEqual(attempt.try_admit(self.allowances), "unresolved")
        self.assert_remaining_questions(9)

    def test_waiting_for_shared_guard_allows_withdrawal_to_win_without_charge(self):
        attempt = self.attempt()
        shared_guard = threading.Lock()
        gate_waiting = threading.Event()
        self.allowances = RollingAllowances(
            self.storage, ObservedLock(shared_guard, gate_waiting.set)
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            shared_guard.acquire()  # Prevent the gate from acquiring its first guard.
            try:
                gate = executor.submit(attempt.try_admit, self.allowances)
                self.assertTrue(gate_waiting.wait(3))
                withdrawal = executor.submit(attempt.withdraw)
                withdrawal.result(3)  # Must not need the shared guard or a held attempt lock.
            finally:
                shared_guard.release()  # A wrong lock order must fail without hanging cleanup.
            self.assertEqual(gate.result(3), "stopped")
        self.assert_remaining_questions(10)

    def test_clock_is_resampled_after_waiting_for_either_guard(self):
        for waiting_on in ("shared", "attempt"):
            with self.subTest(waiting_on=waiting_on):
                attempt = self.attempt()
                self.now = 114.0  # Time at entry is insufficient evidence for admission.
                blocked_guard = threading.Lock()
                waiting = threading.Event()
                observed = ObservedLock(blocked_guard, waiting.set)
                if waiting_on == "shared":
                    self.allowances = RollingAllowances(self.storage, observed)
                else:
                    self.allowances = RollingAllowances(self.storage, threading.Lock())
                    attempt._lock = observed  # Instrument acquisition, not application state.
                with ThreadPoolExecutor(max_workers=1) as executor:
                    blocked_guard.acquire()
                    try:
                        gate = executor.submit(attempt.try_admit, self.allowances)
                        self.assertTrue(waiting.wait(3))
                        self.now = 115.0  # Expire while the gate waits for the selected guard.
                    finally:
                        blocked_guard.release()
                    self.assertEqual(gate.result(3), "stopped")
                self.assertFalse(attempt.charged)
                self.assertIsNone(attempt.admission)
        self.assert_remaining_questions(10)

    def test_commit_holds_attempt_guard_until_timing_record_precedes_withdrawal(self):
        attempt = self.attempt()
        role = threading.local()
        withdrawal_waiting = threading.Event()
        withdrawal_finished = threading.Event()
        record_entered = threading.Event()
        finish_record = threading.Event()
        native_record = admission_module.QuestionAdmission

        def observe_attempt():
            if getattr(role, "value", None) == "withdrawal":
                withdrawal_waiting.set()  # Prove withdrawal reached the contested guard.

        attempt._lock = ObservedLock(threading.Lock(), observe_attempt)

        def pause_record(admitted_at):
            record_entered.set()  # Native True has returned, but recording is not finished.
            if not finish_record.wait(3):
                raise TimeoutError("The test did not release the timing record.")
            return native_record(admitted_at)

        def withdraw():
            role.value = "withdrawal"
            attempt.withdraw()
            withdrawal_finished.set()

        with patch.object(admission_module, "QuestionAdmission", side_effect=pause_record):
            with ThreadPoolExecutor(max_workers=2) as executor:
                gate = executor.submit(attempt.try_admit, self.allowances)
                try:
                    self.assertTrue(record_entered.wait(3))
                    withdrawal = executor.submit(withdraw)
                    self.assertTrue(withdrawal_waiting.wait(3))
                    self.assertFalse(withdrawal_finished.is_set())
                finally:
                    finish_record.set()  # Never leave a test-owned boundary blocked.
                self.assertEqual(gate.result(3), "admitted")
                withdrawal.result(3)
        self.assertEqual(attempt.admission.admitted_at, 100.0)
        self.assertTrue(attempt.charged)
        self.assertTrue(attempt.stopped)
        self.assertFalse(attempt.authorize_normal_work())
        self.assert_remaining_questions(9)

    def test_concurrent_repeated_decisions_share_one_native_charge(self):
        attempt = self.attempt()
        ready = threading.Barrier(12)

        def decide():
            ready.wait(3)  # Start simultaneous calls without timing-based sleeps.
            return attempt.try_admit(self.allowances)

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda _: decide(), range(12)))
        self.assertEqual(results, ["admitted"] * 12)
        self.assert_remaining_questions(9)


class QuestionHTTPSelectionTests(unittest.TestCase):
    """An old result cannot bypass a later deadline or recorded request withdrawal."""

    def setUp(self):
        self.now = 100.0
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.allowances = RollingAllowances(self.storage, threading.Lock())
        self.attempt = admission_module.QuestionAttempt(100.0, clock=lambda: self.now)

    def unresolved_result(self):
        return admission_module.QuestionAdmissionResult("unresolved", None, None, None)

    def test_completed_unresolved_result_cannot_override_expired_current_phase(self):
        self.now = 115.0
        self.assertEqual(self.attempt.http_state(self.unresolved_result()).code, "request_timeout")

    def test_completed_unresolved_result_cannot_override_recorded_withdrawal(self):
        self.attempt.withdraw()
        self.assertEqual(self.attempt.http_state(self.unresolved_result()).code, "request_stopped")

    def test_unknown_hit_stopped_flag_alone_remains_technical_error(self):
        with patch.object(self.allowances, "_try_commit_locked", side_effect=RuntimeError("private")):
            self.assertEqual(self.attempt.try_admit(self.allowances), "unresolved")
        self.assertTrue(self.attempt.stopped)
        self.assertEqual(self.attempt.http_state(self.unresolved_result()).code, "technical_error")


# Overall flow: hold real work -> attempt admission/wait/cancel -> prove capacity effects.
