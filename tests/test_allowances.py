# Python standard library v3.12; limits v5.8.0 stays real at the clock boundary.
import threading  # Release simultaneous callers without timing-based sleeps.
import unittest  # Keep the existing offline test runner.
from concurrent.futures import ThreadPoolExecutor  # Exercise real concurrent commits.
from types import SimpleNamespace  # Replace only the storage module's clock reference.
from unittest.mock import patch  # Advance time without waiting for a real minute.

import limits.storage.memory as memory_module  # Locate the external time boundary.
from limits import RateLimitItemPerMinute  # Seed the expiry-race fixture through public APIs.
from limits.storage import MemoryStorage  # Exercise the actual pinned storage logic.
from limits.strategies import MovingWindowRateLimiter  # Keep fixture timestamp creation real.

from shopunow.allowances import Allowance, RollingAllowances


class ObservedLock:
    """Keep native exclusion while exposing a caller's attempt to acquire it."""

    def __init__(self, lock, before_enter):
        self.lock = lock
        self.before_enter = before_enter

    def __enter__(self):
        self.before_enter()  # Signal before the native acquisition can block.
        return self.lock.__enter__()

    def __exit__(self, *details):
        return self.lock.__exit__(*details)


class ControlledTimer:
    """Let one regression invoke the real expiry callback at an exact interleaving."""

    def __init__(self, interval, function):
        self.function = function  # The callback is still the pinned backend's code.

    def start(self):
        pass  # The regression, rather than elapsed time, schedules this callback.

    def is_alive(self):
        return True  # Avoid scheduling unrelated background callbacks in this fixture.


class AllowanceTests(unittest.TestCase):
    """Protect the approved shared counts and rolling, atomic commit behavior."""

    def setUp(self):
        self.now = 1000.0  # An arbitrary timestamp, deliberately inside a minute.
        clock_patch = patch.object(
            memory_module, "time", SimpleNamespace(time=lambda: self.now)
        )
        clock_patch.start()  # Do not replace Python's process-wide time module.
        self.addCleanup(clock_patch.stop)
        self.storage = MemoryStorage()  # Retain native expiration and key locking.
        self.addCleanup(self.stop_storage_timer)
        self.commit_lock = threading.Lock()  # All wrappers must share this stable lock.
        self.allowances = RollingAllowances(self.storage, self.commit_lock)

    def stop_storage_timer(self):
        # Native storage owns a short-lived timer; cleanup belongs to this test fixture.
        self.storage.timer.cancel()
        self.storage.timer.join(3)

    def test_each_shared_window_rejects_its_first_excess_commit(self):
        # Literal expected counts come from LLD §2, not the implementation's mapping.
        for category, capacity in (
            (Allowance.QUESTION, 10),
            (Allowance.HISTORY, 30),
            (Allowance.PROFILE_READ, 30),
            (Allowance.WRITE, 10),
        ):
            with self.subTest(category=category):
                for _ in range(capacity):
                    self.assertTrue(self.allowances.try_commit(category))
                self.assertFalse(self.allowances.try_commit(category))

    def test_a_new_calendar_minute_does_not_reset_the_question_window(self):
        self.now = 1019.0  # One second before the next calendar-minute boundary.
        for _ in range(10):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.now = 1020.0
        self.assertFalse(self.allowances.try_commit(Allowance.QUESTION))

    def test_staggered_commits_expire_individually_without_charging_rejections(self):
        for _ in range(4):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.now = 1030.0
        for _ in range(6):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.now = 1059.999
        self.assertFalse(self.allowances.try_commit(Allowance.QUESTION))
        self.now = 1060.001  # Only the four oldest admissions have aged past 60 s.
        for _ in range(4):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.assertFalse(self.allowances.try_commit(Allowance.QUESTION))
        self.now = 1090.001  # The six later admissions now expire independently.
        for _ in range(6):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.assertFalse(self.allowances.try_commit(Allowance.QUESTION))

    def test_repeated_rejections_do_not_extend_the_exhausted_window(self):
        for _ in range(10):
            self.assertTrue(self.allowances.try_commit(Allowance.WRITE))
        self.now = 1059.0
        for _ in range(20):
            self.assertFalse(self.allowances.try_commit(Allowance.WRITE))
        self.now = 1060.001
        for _ in range(10):
            self.assertTrue(self.allowances.try_commit(Allowance.WRITE))
        self.assertFalse(self.allowances.try_commit(Allowance.WRITE))

    def test_separate_wrappers_over_shared_storage_cannot_split_the_allowance(self):
        other_caller = RollingAllowances(self.storage, self.commit_lock)
        for _ in range(5):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
            self.assertTrue(other_caller.try_commit(Allowance.QUESTION))
        self.assertFalse(other_caller.try_commit(Allowance.QUESTION))
        self.assertFalse(self.allowances.try_commit(Allowance.QUESTION))

    def test_concurrent_callers_cannot_oversubscribe_any_window(self):
        for category, capacity in (
            (Allowance.QUESTION, 10),
            (Allowance.HISTORY, 30),
            (Allowance.PROFILE_READ, 30),
            (Allowance.WRITE, 10),
        ):
            with self.subTest(category=category):
                # Four slots remain; sixteen simultaneous callers must not all win.
                for _ in range(capacity - 4):
                    self.assertTrue(self.allowances.try_commit(category))
                ready = threading.Barrier(16)

                def commit_together():
                    ready.wait(timeout=3)
                    return self.allowances.try_commit(category)

                with ThreadPoolExecutor(max_workers=16) as executor:
                    results = list(executor.map(lambda _: commit_together(), range(16)))
                self.assertEqual(results.count(True), 4)
                self.assertEqual(results.count(False), 12)

    def test_admitted_failure_and_timeout_do_not_refund_the_question_allowance(self):
        for failure in (RuntimeError, TimeoutError):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
            # The operation ends after admission; its outcome cannot undo the commit.
            with self.assertRaises(failure):
                raise failure("controlled local operation outcome")
        for _ in range(8):
            self.assertTrue(self.allowances.try_commit(Allowance.QUESTION))
        self.assertFalse(self.allowances.try_commit(Allowance.QUESTION))

    def test_expiry_removing_a_native_lock_cannot_admit_an_eleventh_question(self):
        # Instrument scheduling only; retain native locks, expiry, checks and insertion.
        role = threading.local()
        waiting_on_old_lock = threading.Event()
        first_checked = threading.Event()
        second_attempted = threading.Event()
        finish_first = threading.Event()
        self.addCleanup(finish_first.set)

        def observe_commit_attempt():
            if getattr(role, "value", None) == "second":
                second_attempted.set()  # A correct shared guard may block this caller.

        guard = ObservedLock(threading.Lock(), observe_commit_attempt)
        native_threads = SimpleNamespace(RLock=threading.RLock, Timer=ControlledTimer)
        with patch.object(memory_module, "threading", native_threads):
            storage = MemoryStorage()
            first_caller = RollingAllowances(storage, guard)
            second_caller = RollingAllowances(storage, guard)
            for _ in range(10):
                self.assertTrue(first_caller.try_commit(Allowance.QUESTION))
            key = next(iter(storage.events))  # Locate fixture state, not an expected result.
            original_lock = storage.locks[key]

            def observe_native_wait():
                if getattr(role, "value", None) == "first":
                    waiting_on_old_lock.set()

            storage.locks[key] = ObservedLock(original_lock, observe_native_wait)
            self.now = 1060.001  # Every original entry is expired.

            class PausedEntries(list):
                def __getitem__(entries, index):
                    try:
                        return super().__getitem__(index)  # Preserve the real snapshot/error.
                    finally:
                        if index == 9 and getattr(role, "value", None) == "first":
                            first_checked.set()
                            if not finish_first.wait(3):
                                raise TimeoutError("The test did not release the first check.")
                        elif index == 9 and getattr(role, "value", None) == "second":
                            second_attempted.set()  # Unguarded caller checked under a new lock.

            def commit(caller, caller_role):
                role.value = caller_role
                return caller.try_commit(Allowance.QUESTION)

            with ThreadPoolExecutor(max_workers=2) as executor:
                original_lock.acquire()  # Queue one real caller on the old native lock.
                try:
                    first = executor.submit(commit, first_caller, "first")
                    self.assertTrue(waiting_on_old_lock.wait(3))
                    storage.timer.function()  # Native cleanup removes that lock and old hits.
                    # Seed nine fresh admissions through the real native limiter. This is
                    # controlled fixture setup, deliberately outside the application's guard.
                    for _ in range(9):
                        self.assertTrue(MovingWindowRateLimiter(storage).hit(
                            RateLimitItemPerMinute(10), "shopunow", "question"
                        ))
                    storage.events[key] = PausedEntries(storage.events[key])
                finally:
                    original_lock.release()
                try:
                    self.assertTrue(first_checked.wait(3))
                    second = executor.submit(commit, second_caller, "second")
                    self.assertTrue(second_attempted.wait(3))
                finally:
                    finish_first.set()  # Always release test-owned work before shutdown.
                outcomes = [first.result(3), second.result(3)]
            self.assertEqual(outcomes.count(True), 1)
            self.assertEqual(outcomes.count(False), 1)


# Overall flow: control only time -> call real application logic -> inspect admission results.
