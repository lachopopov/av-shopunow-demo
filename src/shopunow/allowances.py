# limits v5.8.0; Python standard library v3.12.
from enum import StrEnum  # Name the four approved traffic categories explicitly.
from contextlib import AbstractContextManager  # Accept a caller-owned native lock.

from limits import RateLimitItemPerMinute  # Express the approved 60-second amounts.
from limits.storage import MemoryStorage  # Compose with caller-owned process-local storage.
from limits.strategies import MovingWindowRateLimiter  # Use exact moving-window entries.


class Allowance(StrEnum):
    """Shared traffic categories, never synthetic-user or conversation keys."""

    QUESTION = "question"
    HISTORY = "history"
    PROFILE_READ = "profile_read"
    WRITE = "write"


class RollingAllowances:
    """Commit only after the caller completes all pre-admission checks.

    The application must share one MemoryStorage and one stable native lock
    within its single worker. The guard keeps commits serialized even when
    native expiration removes a storage key's lock. A False result charges
    nothing; successful commits have no refund on operation failure/timeout.
    This component does not acquire slots, verify owners, reserve resources,
    map HTTP errors, or coordinate other processes or restarts.
    """

    _limits = {
        Allowance.QUESTION: RateLimitItemPerMinute(10),
        Allowance.HISTORY: RateLimitItemPerMinute(30),
        Allowance.PROFILE_READ: RateLimitItemPerMinute(30),
        Allowance.WRITE: RateLimitItemPerMinute(10),
    }

    def __init__(self, storage: MemoryStorage, commit_lock: AbstractContextManager):
        self._limiter = MovingWindowRateLimiter(storage)  # No parallel counter or scheduler.
        self._commit_lock = commit_lock  # The same threading.Lock for all wrappers/storage.

    def try_commit(self, category: Allowance) -> bool:
        """Atomically consume one admission, or return False without waiting for quota."""
        self._limits[category]  # Fail on unsupported internal categories before taking the guard.
        # Category keeps equal numerical limits separate; no changeable user ID is supplied.
        with self._commit_lock:  # Guard only the short local commit, never operation work.
            return self._try_commit_locked(category)

    def _try_commit_locked(self, category: Allowance) -> bool:
        """Same-package admission integration; caller must hold the shared commit guard."""
        item = self._limits[category]  # Retain one native hit and the original category keys.
        return self._limiter.hit(item, "shopunow", category)


# Overall flow: select the shared category -> native atomic moving-window hit -> bool result.
