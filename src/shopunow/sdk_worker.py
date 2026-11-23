# Python 3.12 / AnyIO 4.15.1 / FastAPI 0.143.2; explicitly injected local SDK lifetime.
"""Application-specific client/loop seam; no credentials, record mapping or recovery.

The client has no accepted public pool-cleanup solution yet. Use this component
only in the separately approved local integration, not a production deployment.
"""
from concurrent.futures import ThreadPoolExecutor  # Reuse native scheduling under existing leases.
from contextlib import asynccontextmanager  # Supply FastAPI's native lifespan interface.
from threading import Lock  # Order new submissions against the start of shutdown.
from time import monotonic  # Include bridge scheduling within the caller's remaining budget.
import anyio  # Use supported token bridging and owned shielded thread joining.
from anyio.lowlevel import current_token  # Capture the existing application loop once.
from shopunow.admission import AdmissionBusy  # Preserve the existing safe capacity rejection.


class LocalSDKWorker:
    """One injected SDK client and executor for one FastAPI worker lifetime.

    Existing admission coordinators use this object's submit boundary; their
    ExecutionSlots/leases still own capacity, reservations and tracked outcomes.
    call() is for those admitted synchronous workers, never a new admission path.
    """

    def __init__(self, connect):
        self._connect = connect  # Explicit async SDK initialization; nothing is called here.
        self._pool = ThreadPoolExecutor(max_workers=5)  # The approved fixed business capacity.
        self._guard, self._accepting, self._entered = Lock(), False, False
        self.client, self._token = None, None  # No client or extra loop at import/construction.
        self.stopping, self.drained = anyio.Event(), False  # Operational lifetime state.

    def submit(self, function, /, *args, **kwargs):
        with self._guard:  # No SDK I/O under the short submission/shutdown guard.
            if not self._accepting:
                raise AdmissionBusy('The SDK worker is not accepting work.')
            return self._pool.submit(function, *args, **kwargs)

    def call(self, operation, *, timeout):
        # Capture time before the cross-thread bridge; scheduling never restarts a call budget.
        deadline = monotonic() + min(8.0, timeout)

        async def invoke():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError('The SDK call budget expired.')
            with anyio.fail_after(remaining):
                await anyio.lowlevel.checkpoint()  # Deliver pending cancellation before SDK work.
                return await operation()

        if self._token is None:
            raise RuntimeError('The SDK worker has not started.')
        return anyio.from_thread.run(invoke, token=self._token)  # No portal or per-call loop.

    @asynccontextmanager
    async def lifespan(self, application):
        with self._guard:
            if self._entered:
                raise RuntimeError('The SDK worker lifetime cannot be restarted.')
            self._entered = True  # A closed pool must never be silently reused after tracking loss.
        try:
            with anyio.fail_after(8):  # Bound explicit initialization too.
                self.client = await self._connect()
            self._token = current_token()
            application.state.sdk_worker = self
            with self._guard:
                self._accepting = True
            yield
        finally:
            with self._guard:
                self._accepting = False  # Reject new work before any join/thread-capacity wait.
            self.stopping.set()

            async def join_workers():
                with anyio.CancelScope(shield=True):  # Shield even thread-token acquisition.
                    await anyio.to_thread.run_sync(self._pool.shutdown)
                    self.drained = True  # Native work and completion callbacks have finished.

            async with anyio.create_task_group() as joins:
                joins.start_soon(join_workers)  # Owner cancellation cannot detach the join.
            # No private SDK cleanup. Pool reclamation/production acceptance remains gated.


# Flow: explicit lifespan -> one client/token -> existing admitted native workers
# -> bounded awaited SDK operation -> stop admission -> owned off-loop join.
