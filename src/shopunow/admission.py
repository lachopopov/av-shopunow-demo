# Python standard library v3.12; process-local admission with completion-owned capacity.
from contextlib import nullcontext  # Native optional tracking guard; no parallel lock framework.
import asyncio  # Bridge a worker Future into an asynchronous caller's wait.
import anyio  # Offload attempt-lock waits without abandoning owned cleanup.
from collections.abc import Callable  # Describe only the narrow worker-finished observation.
from concurrent.futures import (
    CancelledError, Future, InvalidStateError, ThreadPoolExecutor, TimeoutError,
)  # Native lifecycle and caller-only wait outcomes.
from dataclasses import dataclass, field, replace  # Native immutable result snapshots.
from datetime import datetime, timezone  # Capture an aware creation time, independently of budgets.
from enum import StrEnum  # Keep decisions explicit without a second business Future.
from threading import BoundedSemaphore, Event, Lock  # Bound capacity and serialize resource claims.
from time import monotonic  # Keep visible attempt deadlines independent of wall-clock changes.
from typing import Literal, Protocol  # Describe injected native-read boundaries, not an SDK adapter.
from uuid import UUID, uuid4  # Normalize selectors and issue backend conversation identities.
from pydantic import TypeAdapter  # Reuse the existing strict synthetic ID contract.

from shopunow.allowances import Allowance, RollingAllowances  # Reuse native quota storage.
from shopunow.records import (
    verify_complete_transcript, verify_owned_conversation_header, verify_profile,
    verify_transcript_events, ConversationHeader, verify_created_conversation_header,
)  # Assemble existing pure record checks without a second parser.
from shopunow.schemas import (
    HistoryResponse, HistorySelectors, QuestionRequest, UserId, ConversationId, InteractionResponse,
)  # Reuse native input and public response models without importing api's projector.


from shopunow.schemas import IdentityReadSelectors, ProfileResolveResponse, PreferenceReadResponse
from shopunow.records import verify_preference
from shopunow.schemas import (ConversationListResponse, ConversationReference,
                             PreferenceUpdateSelectors, PreferenceUpdateResponse)
from shopunow.records import (PreferenceRecord, verify_conversation_headers,
                             verify_updated_preference)

class QuestionDecision(StrEnum):
    """One final quota decision; stopped/unresolved attempts cannot authorize work."""

    ADMITTED = "admitted"
    REJECTED = "rejected"
    STOPPED = "stopped"
    UNRESOLVED = "unresolved"
    HELD = "held"


@dataclass(frozen=True)
class QuestionAdmission:
    """Immutable monotonic timing, without claiming validation or ownership checks."""

    admitted_at: float  # Sampled after both guards are held, immediately before the hit.

    @property
    def question_deadline(self) -> float:
        return self.admitted_at + 90.0  # Visible budget; never a timer-owned lease release.

    @property
    def department_cutoff(self) -> float:
        return self.admitted_at + 60.0  # Membership policy belongs to the later workflow.


@dataclass(frozen=True)
class QuestionHTTPState:
    """One guarded observation of existing phase/deadline/selection state."""

    remaining: float  # Remaining current phase budget, sampled under the attempt guard.
    code: str | None  # Fixed terminal control code, never content or charge evidence.


class QuestionAttempt:
    """Guard one question's quota decision, withdrawal, and logical work permission.

    The caller samples started_at before slot acquisition and completes validation,
    stored-profile/header checks, reservation, transcript checks and two-event room
    before calling try_admit. This gate performs none of those checks. Its 15-second
    pre-admission target is provisional and unmeasured; 15 + 90 is no hard wall time.
    All clocks must be the same monotonic clock. No I/O, worker waits, dispatch,
    retries, refunds or lease/resource release happen in this component.
    """

    def __init__(self, started_at: float, *, clock=monotonic):
        self._lock = Lock()  # Only short synchronous state changes belong under this lock.
        self._clock = clock  # The caller supplies started_at from the same monotonic clock.
        self._pre_admission_deadline = started_at + 15.0  # Approved, still unmeasured target.
        self._stopped = False  # A stop never releases a lease or resource reservation.
        self._withdrawal_recorded = False  # Gate uncertainty alone is not an explicit request stop.
        self._charged: bool | None = False  # None means a hit's charge is unknown.
        self._admission: QuestionAdmission | None = None  # Timing follows a successful hit.
        self._decision: QuestionDecision | None = None  # At most one quota decision.
        self._hold_tracking = None
        self._hold_id = None
        self._held = False

    @property
    def charged(self) -> bool | None:
        with self._lock:
            return self._charged  # Read charge evidence under the same attempt guard.

    @property
    def admission(self) -> QuestionAdmission | None:
        with self._lock:
            return self._admission  # No mutable state is exposed to the caller.

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped  # Cleanup and tracking remain permitted after a stop.

    def try_admit(self, allowances: RollingAllowances) -> QuestionDecision:
        """Decide once under shared-allowance then attempt guard, without releasing either.

        UNRESOLVED plus charged=None means the native hit outcome is unknown;
        charged=True means the hit succeeded but admission recording failed.
        Neither permits retry/refund. Admission alone does not authorize work:
        call authorize_normal_work after the hit, before each new normal step.
        """
        with allowances._commit_lock:  # Always take the stable shared guard first.
            with self._lock:  # Withdrawal will use only this per-attempt guard.
                with self._hold_tracking._lock if self._hold_tracking is not None else nullcontext():
                    if self._decision is not None:
                        return self._decision  # Repeated calls must never hit the quota again.
                    admitted_at = self._clock()  # Recheck after any wait for either guard.
                    if self._stopped or admitted_at >= self._pre_admission_deadline:
                        self._stopped = True
                        self._decision = QuestionDecision.STOPPED
                        return self._decision
                    if self._hold_tracking is not None and self._hold_tracking._excludes_locked((self._hold_id,)):
                        self._held = self._stopped = True
                        self._decision = QuestionDecision.HELD
                        return self._decision
                    # Default to uncertainty before the boundary: even an interrupted hit
                    # cannot leave this attempt eligible for an automatic second charge.
                    self._charged = None
                    self._decision = QuestionDecision.UNRESOLVED
                    self._stopped = True
                    try:
                        committed = allowances._try_commit_locked(Allowance.QUESTION)
                        if committed is True:
                            self._charged = True  # Preserve known charge before record creation.
                            self._admission = QuestionAdmission(admitted_at)
                            self._decision = QuestionDecision.ADMITTED
                            self._stopped = False
                        elif committed is False:
                            self._charged = False  # Only the native Boolean proves rejection.
                            self._decision = QuestionDecision.REJECTED
                            self._stopped = False
                    except Exception:
                        # Keep stopped/unresolved, retaining True if recording failed after hit.
                        # Process-control BaseExceptions still propagate with that safe state.
                        pass
                    return self._decision

    def withdraw(self) -> None:
        """Record a stop under only the attempt guard; never cancel/free underlying work."""
        with self._lock:
            self._withdrawal_recorded = True
            self._stopped = True  # Physical disconnect timing grants no ordering guarantee.

    def _stop_after_failure(self) -> None:
        """Forbid work without misreporting an internal failure as request withdrawal."""
        with self._lock:
            self._stopped = True  # Preserve any separate withdrawal already recorded.

    def pre_admission_timeout(self) -> float | None:
        """Logically permit one bounded pre-admission step, or stop before it starts.

        The returned transport timeout is at most eight seconds and the remaining
        provisional fifteen-second budget. I/O and reservation happen outside this
        guard: an already authorized call may finish after concurrent withdrawal,
        while its execution lease continues to own capacity and any reservation.
        """
        with self._lock:
            if self._stopped or self._decision is not None:
                return None
            remaining = self._pre_admission_deadline - self._clock()
            if remaining <= 0:
                self._stopped = True
                return None
            return min(8.0, remaining)  # One monotonic source also serves try_admit.

    def authorize_normal_work(self) -> bool:
        """Return logical permission only; this method never dispatches work."""
        return self.normal_work_timeout() is not None

    def normal_work_timeout(self) -> float | None:
        """Authorize one step and capture its remaining budget under the same guards."""
        with self._lock:
            with self._hold_tracking._lock if self._hold_tracking is not None else nullcontext():
                if self._stopped or self._admission is None:
                    return None
                remaining = self._admission.question_deadline - self._clock()
                if remaining <= 0:
                    self._stopped = True  # Charge and timing remain intact after expiration.
                    return None
                if self._hold_tracking is not None and self._hold_tracking._excludes_locked((self._hold_id,)):
                    self._held = self._stopped = True
                    return None  # Known charge remains, but no new normal work starts.
                return remaining  # Concurrent stop may follow this authorization point.

    def http_state(self, result=None) -> QuestionHTTPState:
        """Atomically recheck phase and select stop/expiry before an HTTP result.

        Call in a worker thread: a native allowance hit may hold this guard.
        An eligible hit finishing after fifteen seconds retains its original
        admission time and ninety-second budget; an unknown hit stays technical.
        """
        with self._lock:
            current = self._http_state_locked(result)
            if current.code is not None or self._admission is None:
                return current  # Never replace an ownership rejection with a hold lookup.
            try:
                with self._hold_tracking._lock if self._hold_tracking is not None else nullcontext():
                    held = self._hold_tracking is not None and self._hold_tracking._excludes_locked(
                        (self._hold_id,))
                    return self._http_state_locked(result, held=held)  # Sample after both guard waits.
            except Exception:
                return self._http_state_locked(result, tracking_failed=True)

    def _http_state_locked(self, result=None, *, held=False, tracking_failed=False):
        # Caller owns the attempt guard. A failed eligibility check never proves a hold.
        deadline = (self._admission.question_deadline if self._admission is not None
                    else self._pre_admission_deadline)
        remaining = deadline - self._clock()
        if remaining <= 0:
            self._stopped = True
            code = 'request_timeout'
        elif self._withdrawal_recorded:
            code = 'request_stopped'
        elif self._decision == QuestionDecision.UNRESOLVED or (
            result is not None and result.status == 'unresolved'
        ) or tracking_failed:
            code = 'technical_error'
        elif self._held or held:
            code = 'conversation_unresolved'
        else:
            code = None
        return QuestionHTTPState(max(0.0, remaining), code)


class AdmissionBusy(Exception):
    """No execution capacity is immediately available."""


class ReservationConflict(Exception):
    """A resource is already reserved by an unfinished operation."""


class ResourceReservations:
    """Share one registry; reserve only after the relevant ownership checks.

    Keys are (resource namespace, canonical ID), for example conversation/user/offer.
    Each lease must remain active through all protected work and cleanup. This
    registry supplies no ownership lookup, grouped-key transaction, timeout release,
    HTTP mapping, durable unresolved state, or coordination across processes.
    """

    def __init__(self):
        self._lock = Lock()  # A short local critical section, never an operation wait.
        self._owners: dict[tuple[str, str], Future] = {}  # Keep only currently held keys.

    def reserve(self, lease: "ExecutionLease", resource: tuple[str, str]) -> None:
        """Claim one key or fail immediately; completion alone owns its release."""
        completion = lease._completion  # Same-module access preserves the private Future API.
        with self._lock:
            if completion.done():
                raise InvalidStateError("A finished operation cannot reserve a resource.")
            if resource in self._owners:
                raise ReservationConflict("The resource is already reserved.")
            self._owners[resource] = completion  # Check and claim together, without a queue.

        def release(finished: Future):
            with self._lock:
                if self._owners.get(resource) is finished:
                    del self._owners[resource]  # Remove only this operation's own claim.

        # Future may invoke a newly added callback inline. Register outside the lock:
        # completion during registration releases immediately without self-deadlock.
        completion.add_done_callback(release)


class ExecutionLease:
    """Expose waiting and cancellation without exposing Future mutation."""

    def __init__(self, completion: Future):
        self._completion = completion  # Keep state mutation private to admitted work.
        self._capacity_released = Event()  # Evidence set only after actual native slot release.

    @property
    def capacity_released(self):
        return self._capacity_released.is_set()

    def result(self, timeout: float | None = None):
        """Wait synchronously; a wait timeout does not cancel the operation."""
        return self._completion.result(timeout)

    def cancel(self) -> bool:
        """Confirm that the callable cannot start; running work is not cancelled."""
        return self._completion.cancel()

    def add_done_callback(self, callback) -> None:
        """Observe completion through this read-only lease, never the mutable Future."""
        self._completion.add_done_callback(lambda finished: callback(self))

    async def wait(self, timeout: float | None = None):
        """Bound only this wait; caller cancellation cannot cancel underlying work."""
        waiting = asyncio.wrap_future(self._completion)  # Use the current caller loop.
        # Shield may detach a cancelled waiter; retrieve any later bridge exception.
        waiting.add_done_callback(
            lambda finished: None if finished.cancelled() else finished.exception()
        )
        return await asyncio.wait_for(asyncio.shield(waiting), timeout)


class ExecutionSlots:
    """Share one instance across the process's top-level operation types.

    The caller owns its thread executor. Each synchronous callable must include
    all child work and cleanup before returning; detached work is not tracked.
    A worker must not call result() or wait() on its own lease: completion requires
    that worker to return, so waiting for itself would prevent completion.
    There is no admission queue, timer release, shutdown reset, or crash recovery.
    """

    def __init__(self):
        self._available = BoundedSemaphore(5)  # The approved process-wide capacity.

    def submit(self, executor: ThreadPoolExecutor, work, /, *args, **kwargs):
        """Admit immediately or raise AdmissionBusy without dispatching work."""
        return self._submit(executor, work, False, *args, **kwargs)

    def submit_with_lease(self, executor: ThreadPoolExecutor, work, /, *args, **kwargs):
        """Invoke work(lease, *args, **kwargs) with the identical returned lease.

        The lease exists before dispatch, including inline execution. The worker
        may reserve resources with it but must never wait for its own completion.
        """
        return self._submit(executor, work, True, *args, **kwargs)

    def _submit(self, executor, work, pass_lease, /, *args, **kwargs):
        """Keep both invocation forms under the same capacity and completion gate."""
        if not self._available.acquire(blocking=False):
            raise AdmissionBusy("All execution slots are occupied.")

        completion = Future()  # Native atomic pending -> running/cancelled gate.
        lease = ExecutionLease(completion)  # Available even when dispatch runs inline.
        def release_capacity(finished):
            self._available.release()
            lease._capacity_released.set()  # A completed Future alone is not release evidence.
        completion.add_done_callback(release_capacity)

        def run():
            # Successful cancellation prevents invocation even if dispatch queued us.
            if not completion.set_running_or_notify_cancel():
                return
            try:
                if pass_lease:
                    result = work(lease, *args, **kwargs)  # The same completion owner.
                else:
                    result = work(*args, **kwargs)  # Preserve the original invocation.
            except BaseException as error:
                completion.set_exception(error)  # Failure also ends the capacity lease.
            else:
                completion.set_result(result)  # Release only after the callable returns.

        try:
            dispatched = executor.submit(run)  # Use native scheduling, not a new queue.
        except BaseException:
            # submit can fail after dispatch: cancel only if run has not started.
            # If already running, its completion callback retains ownership instead.
            completion.cancel()
            raise

        def dispatch_finished(finished: Future):
            # Shutdown can cancel a queued wrapper, or initialization can fail it.
            if finished.cancelled():
                completion.cancel()  # Native cancellation confirms no wrapper start.
            elif not completion.done() and finished.exception() is not None:
                error = finished.exception()
                # Atomically settle failure versus cancellation; no check/set race.
                if completion.set_running_or_notify_cancel():
                    completion.set_exception(error)
            # An unexplained non-execution stays pending: never assume work stopped.

        dispatched.add_done_callback(dispatch_finished)  # Safe even if already done.
        return lease  # No raw Future or manual-release escape.


class QuestionReaders(Protocol):
    """Injected synchronous reads return untrusted native flat get-shaped results.

    Each call must finish all its work/cleanup before returning and honor its
    supplied transport budget without unbounded retries. The events method does
    one logical scan, includes every returned owner/type, and accepts cap + 1 as
    its explicit maximum. Actual Cloud pagination, completeness and timing remain
    a separate PoC gate; this protocol supplies no speculative client adapter.
    """

    def profile(self, user_id: str, *, timeout: float) -> object: ...

    def header(self, conversation_id: str, *, timeout: float) -> object: ...

    def events(self, conversation_id: str, *, limit: int, timeout: float) -> object: ...

    def exact_offer(self, offer_id: str, *, timeout: float) -> object: ...


@dataclass(frozen=True)
class QuestionAdmissionResult:
    """Local admission metadata and optional validated Slice 2 terminal result.

    A missing quota_decision means this call stopped before the allowance gate.
    charged=None means its native hit outcome is unknown; a known successful hit
    stays charged even if later permission fails. Admitted alone is not an answer;
    repr hides the optional interaction, and final HTTP authorization is separate.
    """

    status: Literal[
        "admitted", "profile_unavailable", "ownership_failure", "conversation_busy",
        "conversation_unresolved", "conversation_full", "quota_exhausted", "stopped",
        "unresolved", "conversation_read_only",
    ]
    charged: bool | None
    quota_decision: QuestionDecision | None
    admission: QuestionAdmission | None
    response_style: Literal['brief', 'detailed'] | None = None  # Captured enum, never saved.
    interaction: InteractionResponse | None = field(default=None, repr=False)


@dataclass(frozen=True)
class QuestionAdmissionHandle:
    """One existing lease and attempt; caller withdrawal never frees running work.

    This synchronous handle detects only its own wait timeout/cancellation. It
    makes no ASGI disconnect-detection claim and adds no watcher or business Future.
    """

    lease: ExecutionLease
    attempt: QuestionAttempt

    def result(self, timeout: float | None = None) -> QuestionAdmissionResult:
        try:
            return self.lease.result(timeout)
        except (TimeoutError, CancelledError):
            self.withdraw()  # Stop downstream permission, retaining native completion ownership.
            raise

    def withdraw(self) -> None:
        self.attempt.withdraw()  # Running reads and cleanup retain the lease/key until return.

    def cancel(self) -> bool:
        self.withdraw()  # Also stop an already running callable when cancel cannot prevent start.
        return self.lease.cancel()  # Only confirmed prevention permits early completion release.


def submit_question_admission(
    request: QuestionRequest | object, *, slots: ExecutionSlots,
    reservations: ResourceReservations, allowances: RollingAllowances,
    executor: ThreadPoolExecutor, readers: QuestionReaders, clock=monotonic, cap: int = 50, tracking=None,
    on_admitted=None,
) -> QuestionAdmissionHandle:
    """Submit one synchronous, locally simulated-capable question admission flow.

    Callers share the slots, reservations, allowance storage/guard and executor.
    The caller owns executor shutdown; never construct/shutdown a per-request
    pool here. Validation precedes the attempt clock and immediate slot attempt.
    Without on_admitted, reads perform no downstream work. An explicitly injected
    continuation runs inside the same admitted lease/reservation; no retries or refunds.
    """
    attempt, run = _prepare_question_admission(
        request, reservations=reservations, allowances=allowances, readers=readers,
        clock=clock, cap=cap, tracking=tracking, on_admitted=on_admitted,
    )  # Both public entries use the identical validation and worker closure.
    try:
        lease = slots.submit_with_lease(executor, run)
    except BaseException:
        attempt.withdraw()  # Preserve the original synchronous control contract.
        raise
    return QuestionAdmissionHandle(lease, attempt)


async def submit_question_admission_async(
    request: QuestionRequest | object, *, slots: ExecutionSlots,
    reservations: ResourceReservations, allowances: RollingAllowances,
    executor: ThreadPoolExecutor, readers: QuestionReaders, clock=monotonic, cap: int = 50, tracking=None,
    on_worker_finished: Callable[[QuestionAdmissionResult | None], None] | None = None,
    on_admitted=None,
) -> QuestionAdmissionHandle:
    """Decide native business capacity before any await or thread-token queue.

    Successful submission returns the existing handle without suspension. Only
    dispatch-error withdrawal can wait for an attempt lock, so only that path is
    offloaded. The ASGI owner must retain this child until its cleanup finishes.
    Native executor short locks/thread creation remain synchronous here. An optional
    narrow observer receives only the immutable result (or None on worker failure)
    after work/cleanup; this is not a native Future/capacity-release notification.
    """
    attempt, run = _prepare_question_admission(
        request, reservations=reservations, allowances=allowances, readers=readers,
        clock=clock, cap=cap, tracking=tracking, on_admitted=on_admitted,
    )

    def observed_run(lease):
        result = None
        try:
            result = run(lease)  # Keep all business work in the existing lease-owned callable.
            return result
        finally:
            if on_worker_finished is not None:
                try:
                    on_worker_finished(result)  # Also runs when dispatch later reports an error.
                except Exception:
                    pass  # A telemetry sink failure cannot change the business result.

    try:
        lease = slots.submit_with_lease(executor, observed_run)  # Immediate business-slot decision.
    except BaseException:
        with anyio.CancelScope(shield=True):  # Protect limiter acquisition before its first await.
            await anyio.to_thread.run_sync(attempt.withdraw, abandon_on_cancel=False)
        raise  # Preserve dispatch failure even when work already began without a returned handle.
    return QuestionAdmissionHandle(lease, attempt)


def _prepare_question_admission(
    request, *, reservations, allowances, readers, clock, cap, tracking=None, on_admitted=None,
):
    """Snapshot selectors and prepare the shared completion-owned callable."""
    # Revalidate even a mutable native model, then capture immutable scalar selectors.
    validated = QuestionRequest.model_validate(
        request.model_dump() if isinstance(request, QuestionRequest) else request,
    )
    if type(cap) is not int or cap < 1:
        raise ValueError("The event cap must be a positive integer.")
    user_id = validated.user_id
    conversation_id = str(UUID(validated.conversation_id))
    response_style = validated.response_style  # Capture before dispatch; request mutation cannot alter it.
    attempt = QuestionAttempt(clock(), clock=clock)  # Capture before non-queuing slot acquisition.
    attempt._hold_tracking, attempt._hold_id = tracking, conversation_id

    def run(lease: ExecutionLease) -> QuestionAdmissionResult:
        decision: QuestionDecision | None = None  # No quota check before all prerequisites pass.

        def outcome(status) -> QuestionAdmissionResult:
            return QuestionAdmissionResult(status, attempt.charged, decision, attempt.admission,
                                           response_style)

        try:
            timeout = attempt.pre_admission_timeout()
            if timeout is None:
                return outcome("stopped")
            profile_result = readers.profile(user_id, timeout=timeout)
            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")  # A late raw result cannot authorize parsing/another read.
            profile = verify_profile(profile_result, user_id=user_id)
            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")  # Late validation cannot select a visible failure.
            if profile.record is None:
                return outcome("profile_unavailable" if profile.status == "absent" else "unresolved")

            timeout = attempt.pre_admission_timeout()
            if timeout is None:
                return outcome("stopped")
            header_result = readers.header(conversation_id, timeout=timeout)
            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")
            header = verify_owned_conversation_header(
                header_result, user_id=user_id, conversation_id=conversation_id,
            )
            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")
            if header.record is None:
                return outcome("ownership_failure" if header.status == "conflict"
                               else "conversation_unresolved")

            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")
            origin_error = ('conversation_read_only' if tracking is None else
                            tracking.question_origin_error(user_id, conversation_id))
            if attempt.pre_admission_timeout() is None:
                return outcome('stopped')  # Include tracking guard waits in the original budget.
            if origin_error is not None:
                return outcome(origin_error)  # No transcript read, quota hit or new write.
            # The supplied worker lease exists before dispatch; no closure-assigned lease race.
            try:
                reservations.reserve(lease, ("conversation", conversation_id))
            except ReservationConflict:
                if attempt.pre_admission_timeout() is None:
                    return outcome("stopped")  # A late conflict cannot select a visible failure.
                return outcome("conversation_busy")

            timeout = attempt.pre_admission_timeout()  # Stop may precede/during the bounded claim.
            if timeout is None:
                return outcome("stopped")
            held = tracking is not None and tracking.excludes((conversation_id,))
            timeout = attempt.pre_admission_timeout()  # Includes any wait for shared tracking.
            if timeout is None:
                return outcome('stopped')
            if held:
                return outcome('conversation_unresolved')
            event_result = readers.events(conversation_id, limit=cap + 1, timeout=timeout)
            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")
            events = verify_transcript_events(
                event_result, user_id=user_id, conversation_id=conversation_id, cap=cap,
            )  # Validate every row/group before deriving or reading any offer selector.
            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")
            if events.record is None:
                return outcome("conversation_unresolved")

            offer_results: dict[str, object] = {}
            for offer_id in events.record.offer_ids:  # Bounded by the fully checked event cap.
                timeout = attempt.pre_admission_timeout()
                if timeout is None:
                    return outcome("stopped")
                offer_results[offer_id] = readers.exact_offer(offer_id, timeout=timeout)
                if attempt.pre_admission_timeout() is None:
                    return outcome("stopped")

            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")
            transcript = verify_complete_transcript(
                header_result, event_result, user_id=user_id, conversation_id=conversation_id,
                offer_results=offer_results, cap=cap,
            )  # Recheck exact offer/request links, including full-prefix historical offers.
            if attempt.pre_admission_timeout() is None:
                return outcome("stopped")
            if transcript.record is None:
                return outcome("conversation_unresolved")
            if not transcript.record.has_two_event_room:
                return outcome("conversation_full")  # Never hide corrupt full history as full.

            decision = attempt.try_admit(allowances)  # Exactly one native hit; no early precheck.
            if decision == QuestionDecision.REJECTED:
                return outcome("quota_exhausted")
            if decision == QuestionDecision.STOPPED:
                return outcome("stopped")
            if decision == QuestionDecision.HELD:
                return outcome('conversation_unresolved')
            if decision != QuestionDecision.ADMITTED:
                return outcome("unresolved")
            if not attempt.authorize_normal_work():
                return outcome('conversation_unresolved' if attempt.http_state().code ==
                               'conversation_unresolved' else 'stopped')
                # Late hits and holds retain charge but grant no normal work.
            if on_admitted is None:
                return outcome('admitted')  # Preserve Slice 1's injected non-success host.
            interaction = on_admitted(request=validated, profile=profile.record,
                                      transcript=transcript.record, attempt=attempt)
            if not attempt.authorize_normal_work():
                return outcome('stopped')  # Late work never authorizes a prepared result.
            if interaction is not None:
                interaction = InteractionResponse.model_validate_json(
                    interaction.model_dump_json(warnings='error'))
                if (interaction.user_id != user_id or interaction.conversation_id != conversation_id):
                    raise ValueError('Interaction identity does not match admission.')
                # Slice 5: reviewed/code-owned outcomes arrive only after a verified saved terminal.
                if interaction.outcome.kind not in ('confirmed_attack_block', 'fallback_error', 'answer',
                                                    'clarification', 'refusal', 'handoff_offer'):
                    raise ValueError('Unknown interaction outcome.')
            return replace(outcome('admitted'), interaction=interaction)
        except Exception:
            stopped = attempt.stopped or (
                decision is None and attempt.pre_admission_timeout() is None
            )  # A late failure cannot supersede an already stopped pre-admission call.
            attempt._stop_after_failure()  # Failure is not evidence of request withdrawal.
            return outcome("stopped" if stopped else "unresolved")  # No private exception text.

    return attempt, run  # The chosen public entry owns dispatch and exceptional withdrawal.


# Overall flow: validate -> immediate slot -> exact profile/header -> lease-owned key
# -> bounded whole event stage -> exact offers -> complete transcript/room -> one quota
# decision -> post-hit permission -> local control result; actual completion releases resources.


class HistoryDecision(StrEnum):
    """Final HISTORY quota decision, independent of delivery or response readiness."""

    ADMITTED = "admitted"
    REJECTED = "rejected"
    STOPPED = "stopped"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class HistoryState:
    """Content-free guarded observation for a later, separately authorized HTTP owner."""

    remaining: float
    code: str | None


@dataclass(frozen=True)
class HistoryAdmissionResult:
    """Local immutable output; only ready contains data, and ready is not delivery."""

    status: Literal[
        "ready", "profile_unavailable", "ownership_failure", "conversation_unresolved",
        "quota_exhausted", "stopped", "unresolved",
    ]
    charged: bool | None  # None means the hit outcome is unknown, never safe to retry.
    quota_decision: HistoryDecision | None
    response: HistoryResponse | None = field(default=None, repr=False)  # Never print stored text.


class HistoryAttempt:
    """Guard one original fifteen-second deadline, stop and final history allowance hit.

    This component performs no reads, projection, lease release or HTTP waiting.
    All controls may wait for the native attempt lock and belong off an event loop.
    """

    def __init__(self, started_at: float, *, clock=monotonic):
        self._lock = Lock()  # Serialize withdrawal against the final quota decision.
        self._clock = clock
        self._deadline = started_at + 15.0  # No post-admission phase or deadline reset.
        self._stopped = False
        self._withdrawal_recorded = False  # Technical uncertainty is not a request withdrawal.
        self._charged: bool | None = False
        self._decision: HistoryDecision | None = None
        self._owned_tracking = None
        self._owned_selectors = None
        self._owned_ids = ()
        self._owned_response = None

    @property
    def charged(self) -> bool | None:
        with self._lock:
            return self._charged

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def _remaining_locked(self) -> float:
        remaining = self._deadline - self._clock()  # Always sample after lock acquisition.
        if remaining <= 0:
            self._stopped = True
        return max(0.0, remaining)

    def step_timeout(self) -> float | None:
        """Authorize one bounded read/check; an already running step retains its lease."""
        with self._lock:
            remaining = self._remaining_locked()
            if self._stopped or self._decision is not None:
                return None
            return min(8.0, remaining)  # The same clock/budget applies to every step.

    def withdraw(self) -> None:
        with self._lock:
            self._withdrawal_recorded = True
            self._stopped = True  # No capacity release or claim that a running step stopped.

    def _stop_after_failure(self) -> None:
        with self._lock:
            self._stopped = True  # Preserve any factual withdrawal and charge evidence.

    def try_admit(self, allowances: RollingAllowances) -> HistoryDecision:
        """Commit the fixed history category using the shared guarded read mechanics."""
        return self._try_admit_read(allowances, Allowance.HISTORY)

    def _try_admit_read(self, allowances, category) -> HistoryDecision:
        """Same-package reuse for the two fixed read categories; never client-selected."""
        with allowances._commit_lock:  # Shared allowance first, then attempt: never reverse.
            with self._lock:
                if self._decision is not None:
                    return self._decision
                self._remaining_locked()  # A wait on either guard can consume the budget.
                if self._stopped:
                    self._decision = HistoryDecision.STOPPED
                    return self._decision
                self._charged = None  # Default uncertain before the potentially interrupted hit.
                self._decision = HistoryDecision.UNRESOLVED
                self._stopped = True
                try:
                    committed = allowances._try_commit_locked(category)
                    if committed is True:
                        self._charged = True  # A late success stays counted permanently.
                        self._decision = HistoryDecision.ADMITTED
                        self._stopped = False
                    elif committed is False:
                        self._charged = False
                        self._decision = HistoryDecision.REJECTED
                        self._stopped = False
                except Exception:
                    pass  # Retain stopped/unknown without exposing errors or retrying.
                self._remaining_locked()  # True, False and uncertain hits all recheck original time.
                return self._decision

    def outcome(self, status, response: HistoryResponse | None = None) -> HistoryAdmissionResult:
        """Recheck eligibility and snapshot charge/decision/data under one attempt guard."""
        with self._lock:
            remaining = self._remaining_locked()
            if remaining <= 0 or self._withdrawal_recorded or (status == "ready" and self._stopped):
                status = "stopped"  # Expired or withdrawn failures never select an old result.
            if status == 'ready':
                self._owned_response = response
            return HistoryAdmissionResult(
                status, self._charged, self._decision, response if status == "ready" else None,
            )  # No failed or admitted-but-stopped output retains history content.

    def checked_state(self, result: HistoryAdmissionResult | None = None) -> HistoryState:
        """Select timeout/withdrawal/technical mapping without interpreting stopped as closure."""
        with self._lock:
            return _guarded_owned_state(self, result)

    def bind_owned(self, tracking, selectors, ids, response=None):
        with self._lock:
            self._owned_tracking, self._owned_selectors = tracking, selectors
            self._owned_ids = tuple(str(UUID(value)) for value in ids)
            self._owned_response = response

    def _checked_state_locked(self, result=None):
        """Caller holds the attempt guard; identity reads add binding in this same view."""
        remaining = self._remaining_locked()
        if remaining <= 0:
            code = "request_timeout"
        elif self._withdrawal_recorded:
            code = "request_stopped"
        elif self._decision == HistoryDecision.UNRESOLVED or (
            result is not None and result.status == "unresolved"
        ):
            code = "technical_error"
        else:
            code = None
        return HistoryState(remaining, code)


@dataclass(frozen=True)
class HistoryAdmissionHandle:
    """Synchronous wait/control over the existing lease; no extra business Future."""

    lease: ExecutionLease
    attempt: HistoryAttempt

    def result(self, timeout: float | None = None) -> HistoryAdmissionResult:
        try:
            return self.lease.result(timeout)
        except (TimeoutError, CancelledError):
            self.withdraw()  # Stop logical use without abandoning completion-owned cleanup.
            raise

    def withdraw(self) -> None:
        self.attempt.withdraw()

    def cancel(self) -> bool:
        self.withdraw()
        return self.lease.cancel()  # Release early only on confirmed prevention of callable start.


def submit_history_admission(
    selectors: HistorySelectors | object, *, slots: ExecutionSlots,
    allowances: RollingAllowances, executor: ThreadPoolExecutor, readers: QuestionReaders,
    projector: Callable[..., HistoryResponse], clock=monotonic, cap: int = 50, tracking=None,
) -> HistoryAdmissionHandle:
    """Read/verify/project fully, then consume HISTORY and return a local ready result.

    Trusted application assembly injects the existing pure projector; no input can
    choose it. Callers share the existing slots, native limiter/guard and executor.
    No write reservation is acquired: an incomplete observed prefix fails safely.
    Reader transport enforcement, exhaustive Cloud scans and HTTP delivery are not
    established here. Every reader must finish its own work and cleanup on return.
    """
    attempt, run = _prepare_history_admission(
        selectors, allowances=allowances, readers=readers, projector=projector, clock=clock, cap=cap, tracking=tracking,
    )  # Preserve identical validation and synchronous worker behavior for both entries.
    try:
        lease = slots.submit_with_lease(executor, run)
    except BaseException:
        attempt.withdraw()  # Dispatch-after-start failure cannot permit a later allowance hit.
        raise
    return HistoryAdmissionHandle(lease, attempt)


async def submit_history_admission_async(
    selectors: HistorySelectors | object, *, slots: ExecutionSlots,
    allowances: RollingAllowances, executor: ThreadPoolExecutor, readers: QuestionReaders,
    projector: Callable[..., HistoryResponse], clock=monotonic, cap: int = 50, tracking=None,
    on_worker_finished: Callable[[HistoryAdmissionResult | None], None] | None = None,
) -> HistoryAdmissionHandle:
    """Attempt the shared business slot before any await or native thread-token wait.

    The caller owns the HTTP child until dispatch-failure withdrawal completes.
    Observing worker return does not assert that native completion released capacity.
    """
    attempt, run = _prepare_history_admission(
        selectors, allowances=allowances, readers=readers, projector=projector, clock=clock, cap=cap, tracking=tracking,
    )

    def observed_run(lease):
        result = None
        try:
            result = run(lease)  # Retain the one existing completion-owned worker.
            return result
        finally:
            if on_worker_finished is not None:
                try:
                    on_worker_finished(result)  # Includes dispatch failure after work starts.
                except Exception:
                    pass  # A sink cannot grant permission or change the worker result.

    try:
        lease = slots.submit_with_lease(executor, observed_run)
    except AdmissionBusy:
        raise  # No lease or worker exists, so rejection requires no control-thread token.
    except BaseException:
        with anyio.CancelScope(shield=True):  # Cover limiter acquisition as well as guard waiting.
            await anyio.to_thread.run_sync(attempt.withdraw, abandon_on_cancel=False)
        raise
    return HistoryAdmissionHandle(lease, attempt)  # No suspension after successful submission.


def _prepare_history_admission(selectors, *, allowances, readers, projector, clock, cap, tracking=None):
    """Capture validated selectors and the unchanged fifteen-second worker contract."""
    captured = HistorySelectors.model_validate(
        selectors.model_dump(warnings="error") if isinstance(selectors, HistorySelectors) else selectors,
    )  # Native dump/revalidation rejects constructed or forcibly mutated model instances too.
    if type(cap) is not int or cap < 1:
        raise ValueError("The event cap must be a positive integer.")
    user_id, conversation_id = captured.user_id, captured.conversation_id
    attempt = HistoryAttempt(clock(), clock=clock)  # Immediately before non-queuing slot attempt.
    attempt.bind_owned(tracking, captured, (conversation_id,))

    def run(lease: ExecutionLease) -> HistoryAdmissionResult:
        # The existing lease owns every synchronous read, verification and projection through return.
        try:
            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            profile_result = readers.profile(user_id, timeout=timeout)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            profile = verify_profile(profile_result, user_id=user_id)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if profile.record is None:
                return attempt.outcome("profile_unavailable" if profile.status == "absent" else "unresolved")

            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            header_result = readers.header(conversation_id, timeout=timeout)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            header = verify_owned_conversation_header(
                header_result, user_id=user_id, conversation_id=conversation_id,
            )
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if header.record is None:
                return attempt.outcome("ownership_failure" if header.status == "conflict"
                                       else "conversation_unresolved")

            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            if tracking is not None and tracking.excludes((conversation_id,)):
                return attempt.outcome('conversation_unresolved')
            event_result = readers.events(conversation_id, limit=cap + 1, timeout=timeout)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            events = verify_transcript_events(
                event_result, user_id=user_id, conversation_id=conversation_id, cap=cap,
            )  # Validate the whole returned prefix before selecting any offer or doing offer I/O.
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if events.record is None:
                return attempt.outcome("conversation_unresolved")

            offer_results: dict[str, object] = {}
            for offer_id in events.record.offer_ids:
                timeout = attempt.step_timeout()
                if timeout is None:
                    return attempt.outcome("stopped")
                offer_results[offer_id] = readers.exact_offer(offer_id, timeout=timeout)
                if attempt.step_timeout() is None:
                    return attempt.outcome("stopped")

            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            transcript = verify_complete_transcript(
                header_result, event_result, user_id=user_id, conversation_id=conversation_id,
                offer_results=offer_results, cap=cap,
            )  # All per-request exact-offer links pass together, with no partial content.
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if transcript.record is None:
                return attempt.outcome("conversation_unresolved")

            response = projector(transcript.record, selectors=captured)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if not isinstance(response, HistoryResponse):
                raise ValueError("The projector must return the native history response.")
            response = HistoryResponse.model_validate(response.model_dump(warnings="error"))
            # Native errors stay inside this safe failure boundary, rather than warning with text.
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if (response.request_id != captured.request_id or response.user_id != user_id
                    or response.conversation_id != conversation_id):
                raise ValueError("The response does not match the captured selectors.")
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")

            if tracking is not None and tracking.excludes((conversation_id,)):
                return attempt.outcome('conversation_unresolved')
            decision = attempt.try_admit(allowances)  # Only fully verified/projected data reaches hit.
            if decision == HistoryDecision.REJECTED:
                return attempt.outcome("quota_exhausted")
            if decision == HistoryDecision.STOPPED:
                return attempt.outcome("stopped")
            if decision != HistoryDecision.ADMITTED:
                return attempt.outcome("unresolved")
            return attempt.outcome("ready", response)  # Final guarded eligibility, no deadline reset.
        except Exception:
            attempt._stop_after_failure()  # Internal failure is not recorded as caller withdrawal.
            return attempt.outcome("unresolved")  # Deadline/withdrawal still takes priority; no text.

    return attempt, run  # Assembly does not dispatch work or introduce another completion owner.


# History flow: captured native selectors -> immediate shared lease -> exact profile/header
# -> bounded whole events -> exact offers -> complete verification -> native projection/binding
# -> one guarded HISTORY hit -> original-deadline permission -> ready data or content-free failure.
# The callable's actual completion owns capacity release; no HTTP, write or retry is introduced.


@dataclass(frozen=True)
class CreationSnapshot:
    """Observations retained independently of native worker completion."""

    charged: bool | None
    write_possible: bool
    verified: bool

    @property
    def unresolved_write(self) -> bool:
        return self.write_possible and not self.verified  # Never inferred from Future.done().


@dataclass(frozen=True)
class CreationResult:
    """Internal result only; no HTTP delivery or live storage guarantee."""

    status: Literal[
        "ready", "profile_unavailable", "creation_busy", "quota_exhausted", "stopped", "unresolved",
    ]
    charged: bool | None
    request_id: str
    user_id: str
    conversation_id: str | None = None  # Unverified or stopped work exposes no usable ID.


class CreationAttempt:
    """Guard creation's single budget, charge, withdrawal and conservative write state."""

    def __init__(self, started_at, *, clock):
        self._lock = Lock()
        self._clock = clock
        self._deadline = started_at + 15.0  # Provisional cooperative budget; no second phase.
        self._withdrawn = False
        self._stopped = False
        self._charged: bool | None = False
        self._decision = None
        self._write_possible = False
        self._verified = False
        self._owned_tracking = None
        self._owned_ids = ()
        self._owned_selectors = None
        self._owned_response = None

    def _remaining_locked(self):
        remaining = self._deadline - self._clock()  # Resample after waiting for any guard.
        if remaining <= 0:
            self._stopped = True
        return max(0.0, remaining)

    def step_timeout(self):
        with self._lock:
            remaining = self._remaining_locked()
            return None if self._stopped else min(8.0, remaining)

    def withdraw(self):
        with self._lock:
            self._withdrawn = self._stopped = True  # No lease release or uncertainty clearing.

    def snapshot(self) -> CreationSnapshot:
        with self._lock:
            return CreationSnapshot(self._charged, self._write_possible, self._verified)

    def try_admit(self, allowances):
        with allowances._commit_lock:  # Stable allowance guard always precedes attempt guard.
            with self._lock:
                self._remaining_locked()
                if self._stopped:
                    return "stopped"
                if self._decision is not None:
                    return self._decision
                self._charged = None
                self._decision = "unresolved"
                self._stopped = True  # An interrupted hit cannot leave permission to write.
                try:
                    committed = allowances._try_commit_locked(Allowance.WRITE)
                    if committed is True:
                        self._charged = True
                        self._decision = "admitted"
                        self._stopped = False
                    elif committed is False:
                        self._charged = False
                        self._decision = "quota_exhausted"
                        self._stopped = False
                except Exception:
                    pass  # Keep unknown charge, with no retry or exception diagnostics.
                self._remaining_locked()  # A late hit never restarts the original deadline.
                return self._decision

    def authorize_write(self):
        """Called only after retention succeeds; record uncertainty before writer entry."""
        with self._lock:
            return _authorize_owned_write(self)  # Resample only after both guards are held.

    def record_verified(self):
        with self._lock:
            self._verified = True  # Only exact intended-header verification calls this method.
            if self._owned_tracking is not None:
                self._owned_tracking.creation_verified(self._owned_ids[0])

    def bind_response(self, response):
        with self._lock:
            self._owned_response = response

    def checked_state(self, result=None):
        with self._lock:
            return _guarded_owned_state(self, result)

    def _checked_state_locked(self, result=None):
        remaining = self._remaining_locked()
        if remaining <= 0:
            code = 'request_timeout'
        elif self._withdrawn:
            code = 'request_stopped'
        elif self._charged is None or (result is not None and result.status == 'unresolved'):
            code = 'technical_error'
        else:
            code = None
        return HistoryState(remaining, code)

    def select_result(self, result: CreationResult) -> CreationResult:
        """Recheck a previously completed result before exposing it to the local caller."""
        with self._lock:
            if self._remaining_locked() <= 0 or self._withdrawn:
                return replace(result, status="stopped", conversation_id=None)
            return result  # Later HTTP integration still needs its own outbound authorization.

    def outcome(self, status, *, request_id, user_id, header=None):
        with self._lock:
            remaining = self._remaining_locked()
            if remaining <= 0 or self._withdrawn:
                status = "stopped"
            if status == "ready" and (self._stopped or not self._verified):
                status = "unresolved"
            return CreationResult(
                status, self._charged, request_id, user_id,
                str(header.conversation_id) if status == "ready" else None,
            )  # Known saved-but-stopped attempts never return a usable conversation ID.


@dataclass(frozen=True)
class CreationIntent:
    """Caller-owned supervisor must retain this before any writer may run."""

    request_id: str
    header: ConversationHeader = field(repr=False)
    attempt: CreationAttempt = field(repr=False)
    lease: ExecutionLease = field(repr=False)  # Retain completion tracking even on dispatch error.


class CreationStorage(Protocol):
    """Injected local boundaries; each call owns all descendants/cleanup until return.

    Write acknowledgement proves no save. Live completion, create-only semantics,
    retries and transport enforcement require the separate CloudClient PoC.
    """

    def profile(self, user_id: str, *, timeout: float) -> object: ...
    def write_header(self, header: ConversationHeader, *, timeout: float) -> object: ...
    def header(self, conversation_id: str, *, timeout: float) -> object: ...


class CreationSupervisor(Protocol):
    """Trusted caller-owned local retention, not a production recovery registry.

    retain must synchronously retain a strong reference or raise, reject ID reuse,
    and exclude unresolved IDs from exposure. Completion must not discard intent.
    The caller owns supervisor lifetime; there is no default or fallback owner.
    """

    def retain(self, intent: CreationIntent) -> None: ...


@dataclass(frozen=True)
class CreationHandle:
    lease: ExecutionLease
    attempt: CreationAttempt

    def result(self, timeout=None):
        try:
            return self.attempt.select_result(self.lease.result(timeout))
        except (TimeoutError, CancelledError):
            self.withdraw()
            raise

    def withdraw(self):
        self.attempt.withdraw()

    def cancel(self):
        self.withdraw()
        return self.lease.cancel()  # Only confirmed prevention releases unstarted work.


def _prepare_conversation_creation(
    *, user_id: str, request_id: str, slots: ExecutionSlots, reservations: ResourceReservations,
    allowances: RollingAllowances, executor: ThreadPoolExecutor, storage: CreationStorage,
    supervisor: CreationSupervisor, clock=monotonic, uuid_factory=uuid4,
    utc_now=lambda: datetime.now(timezone.utc), tracking=None,
) -> CreationHandle:
    """Local synchronous creation only; no route, credentials, SDK or recovery algorithm.

    The caller must supply a retaining local supervisor and shared single-process
    dependencies. Native completion owns active capacity; retained attempt state
    owns unresolved outcome evidence independently, with no automatic reconciliation.
    """
    selected_user = TypeAdapter(UserId).validate_python(user_id, strict=True)
    selected_request = TypeAdapter(ConversationId).validate_python(request_id, strict=True)
    attempt = CreationAttempt(clock(), clock=clock)  # Immediately before non-queuing submission.

    def run(lease):
        def outcome(status, header=None):
            return attempt.outcome(status, request_id=selected_request,
                                   user_id=selected_user, header=header)

        try:
            timeout = attempt.step_timeout()
            if timeout is None:
                return outcome("stopped")
            native = storage.profile(selected_user, timeout=timeout)
            if attempt.step_timeout() is None:
                return outcome("stopped")
            profile = verify_profile(native, user_id=selected_user)
            if attempt.step_timeout() is None:
                return outcome("stopped")
            if profile.record is None:
                return outcome("profile_unavailable" if profile.status == "absent" else "unresolved")
            try:
                reservations.reserve(lease, ("user", selected_user))
            except ReservationConflict:
                return outcome("creation_busy")
            if attempt.step_timeout() is None:
                return outcome("stopped")
            decision = attempt.try_admit(allowances)
            if decision != "admitted":
                return outcome(decision)
            if attempt.step_timeout() is None:
                return outcome("stopped")
            intended = ConversationHeader(
                record_type="conversation_header", owner_user_id=selected_user,
                conversation_id=uuid_factory(), created_at=utc_now(), complete=True,
            )  # Capture independent backend identity and time; caller chooses neither.
            if attempt.step_timeout() is None:
                return outcome("stopped")
            supervisor.retain(CreationIntent(selected_request, intended, attempt, lease))
            # Prepare a detached writer argument before conservative write authorization.
            writing = ConversationHeader.model_validate(intended.model_dump(warnings="error"))
            timeout = attempt.authorize_write()
            if timeout is None:
                return outcome("stopped")
            storage.write_header(writing, timeout=timeout)  # Exactly one call; ignore ACK.
            timeout = attempt.step_timeout()
            if timeout is None:
                return outcome("stopped")
            native = storage.header(str(intended.conversation_id), timeout=timeout)
            if attempt.step_timeout() is None:
                return outcome("stopped")
            checked = verify_created_conversation_header(native, expected=intended)
            if checked.record is None:
                return outcome("unresolved")  # Retained possible-write state stays unresolved.
            attempt.record_verified()  # Evidence recording is distinct from success eligibility.
            return outcome("ready", intended)
        except Exception:
            return outcome("unresolved")  # Preserve intent/charge without exception text or retry.

    return attempt, run


def submit_conversation_creation(**options):
    """Preserve the synchronous entry and completion-owned native dispatch."""
    attempt, run = _prepare_conversation_creation(**options)
    try:
        lease = options['slots'].submit_with_lease(options['executor'], run)
    except BaseException:
        attempt.withdraw()
        raise
    return CreationHandle(lease, attempt)


async def submit_conversation_creation_async(*, on_worker_finished=None, **options):
    attempt, run = _prepare_conversation_creation(**options)
    return await _submit_owned_async(attempt, run, options['slots'], options['executor'],
                                     CreationHandle, on_worker_finished)


# Creation flow: validate -> slot -> profile -> user reservation -> WRITE -> retain intent
# -> conservatively authorize once -> write -> exact intended read-back -> eligible result.
# Completion releases active work only; the caller-owned supervisor retains unresolved intent.


# Identity reads reuse the already demonstrated single-budget guarded read lifecycle.


@dataclass(frozen=True)
class IdentityReadResult:
    status: str  # Internal fixed codes; never a storage exception or returned content.
    charged: bool | None
    response: ProfileResolveResponse | PreferenceReadResponse | None = field(default=None, repr=False)


class IdentityReadAttempt(HistoryAttempt):
    """Reuse original 15s read budget/locks; fix the shared PROFILE_READ category."""

    def __init__(self, started_at, *, clock, selectors):
        super().__init__(started_at, clock=clock)
        self._selectors = selectors  # Independently captured before any reader/projector runs.
        self._response = None

    def try_admit(self, allowances):
        return self._try_admit_read(allowances, Allowance.PROFILE_READ)

    def checked_state(self, result=None):
        with self._lock:
            state = _guarded_owned_state(self, result)
            response = self._response
            if state.code is None and response is not None and (
                response.user_id != self._selectors.user_id
                or response.request_id != self._selectors.request_id
                or (isinstance(response, ProfileResolveResponse)
                    and response.profile.user_id != self._selectors.user_id)
            ):
                return HistoryState(state.remaining, 'technical_error')
            return state

    def outcome(self, status, response=None):
        with self._lock:
            if (self._remaining_locked() <= 0 or self._withdrawal_recorded
                    or (status == 'ready' and self._stopped)):
                status = 'stopped'
            if status == 'ready':
                self._response = response  # Final HTTP check inspects the same serialized model.
                if self._owned_selectors is not None:
                    self._owned_response = response
            return IdentityReadResult(status, self._charged, response if status == 'ready' else None)


@dataclass(frozen=True)
class IdentityReadHandle:
    lease: ExecutionLease
    attempt: IdentityReadAttempt

    def result(self, timeout=None):
        try:
            result = self.lease.result(timeout)
            return self.attempt.outcome(result.status, result.response)
        except (TimeoutError, CancelledError):
            self.withdraw()
            raise

    def withdraw(self):
        self.attempt.withdraw()

    def cancel(self):
        self.withdraw()
        return self.lease.cancel()  # Only native prevention of start can release early.


def _prepare_identity_read(selectors, *, operation, readers, roster, projector, allowances, clock):
    captured = IdentityReadSelectors.model_validate(
        selectors.model_dump(warnings='error') if isinstance(selectors, IdentityReadSelectors)
        else selectors)
    if operation not in ('profile', 'preference'):
        raise ValueError('Unsupported internal read operation.')
    approved_ids = frozenset(TypeAdapter(UserId).validate_python(value, strict=True) for value in roster)
    attempt = IdentityReadAttempt(clock(), clock=clock, selectors=captured)  # Start just before slot acquisition.

    def run(lease):
        try:
            if captured.user_id not in approved_ids:
                return attempt.outcome('profile_unavailable')
            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome('stopped')
            profile = verify_profile(readers.profile(captured.user_id, timeout=timeout),
                                     user_id=captured.user_id)
            if attempt.step_timeout() is None:
                return attempt.outcome('stopped')
            if profile.record is None:
                return attempt.outcome('profile_unavailable' if profile.status == 'absent' else 'unresolved')
            preference = None
            if operation == 'preference':
                timeout = attempt.step_timeout()
                if timeout is None:
                    return attempt.outcome('stopped')
                checked = verify_preference(readers.preference(captured.user_id, timeout=timeout),
                                            user_id=captured.user_id)
                if attempt.step_timeout() is None:
                    return attempt.outcome('stopped')
                if checked.record is None and checked.status != 'absent':
                    return attempt.outcome('unresolved')
                preference = checked.record  # Only a valid exact absence may become null.
            response = projector(profile.record, preference, selectors=captured, operation=operation)
            if attempt.step_timeout() is None:
                return attempt.outcome('stopped')
            model = ProfileResolveResponse if operation == 'profile' else PreferenceReadResponse
            if not isinstance(response, model):
                raise ValueError('Unexpected native response type.')
            response = model.model_validate(response.model_dump(warnings='error'))
            if (response.request_id != captured.request_id or response.user_id != captured.user_id
                    or (operation == 'profile' and response.profile.user_id != captured.user_id)):
                raise ValueError('Response binding failed.')
            if operation == 'profile':
                if (response.created is not False or response.profile.role != profile.record.role
                        or response.profile.home_department != profile.record.home_department):
                    raise ValueError('The projection changed the verified binding.')
            elif response.response_style != (None if preference is None else preference.value):
                raise ValueError('The projection changed the verified preference.')
            decision = attempt.try_admit(allowances)  # Full record and projection validation first.
            status = {HistoryDecision.ADMITTED: 'ready', HistoryDecision.REJECTED: 'quota_exhausted',
                      HistoryDecision.STOPPED: 'stopped'}.get(decision, 'unresolved')
            return attempt.outcome(status, response)
        except Exception:
            attempt._stop_after_failure()  # No exception diagnostics, writes or automatic retry.
            return attempt.outcome('unresolved')
    return attempt, run


def submit_identity_read(selectors, *, operation, slots, allowances, executor, readers,
                         roster, projector, clock=monotonic):
    """Local injected exact reads; caller owns roster provenance and external transport."""
    attempt, run = _prepare_identity_read(
        selectors, operation=operation, readers=readers, roster=roster, projector=projector,
        allowances=allowances, clock=clock)
    try:
        lease = slots.submit_with_lease(executor, run)
    except BaseException:
        attempt.withdraw()
        raise
    return IdentityReadHandle(lease, attempt)


async def submit_identity_read_async(selectors, *, operation, slots, allowances, executor, readers,
                                     roster, projector, clock=monotonic, on_worker_finished=None):
    """Acquire shared business capacity before any control-thread token wait."""
    attempt, run = _prepare_identity_read(
        selectors, operation=operation, readers=readers, roster=roster, projector=projector,
        allowances=allowances, clock=clock)
    def observed_run(lease):
        result = None
        try:
            result = run(lease)
            return result
        finally:
            if on_worker_finished is not None:
                try:
                    on_worker_finished(result)
                except Exception:
                    pass
    try:
        lease = slots.submit_with_lease(executor, observed_run)
    except AdmissionBusy:
        raise
    except BaseException:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(attempt.withdraw, abandon_on_cancel=False)
        raise
    return IdentityReadHandle(lease, attempt)


# Flow: captured selectors/roster -> shared lease -> exact profile/preference -> projection
# -> guarded PROFILE_READ hit -> current permission; native completion still owns resources.


# Owned records: explicit local creation/preference intent tracking, not restart recovery.


def _owned_response_matches(response, selectors, ids):
    """Final identity check against captured selectors, independent of serialized content."""
    try:
        if response.request_id != selectors.request_id or response.user_id != selectors.user_id:
            return False
        if hasattr(response, 'conversations'):
            return tuple(row.conversation_id for row in response.conversations) == ids
        if hasattr(response, 'conversation_id'):
            return (response.conversation_id,) == ids
        return not ids
    except (AttributeError, TypeError):
        return False


def _guarded_owned_state(attempt, result=None):
    """Caller already holds attempt; acquire tracking next and never reverse the order."""
    state = attempt._checked_state_locked(result)
    # Eligibility is a final prepared-success check, not an alternative ownership/admission check.
    if (state.code is not None or (result is not None and result.status != 'ready')
            or attempt._owned_response is None):
        return state
    tracking = attempt._owned_tracking
    try:
        with tracking._lock if tracking is not None else nullcontext():
            state = attempt._checked_state_locked(result)  # Includes time spent waiting for tracking.
            if state.code is not None:
                return state
            if attempt._owned_response is not None and not _owned_response_matches(
                    attempt._owned_response, attempt._owned_selectors, attempt._owned_ids):
                return HistoryState(state.remaining, 'technical_error')
            if tracking is not None and tracking._excludes_locked(attempt._owned_ids):
                return HistoryState(state.remaining, 'conversation_unresolved')
            return state
    except Exception:
        # Tracking loss/failure is uncertainty, never proof of eligibility or no write.
        state = attempt._checked_state_locked(result)
        return state if state.code is not None else HistoryState(state.remaining, 'technical_error')


def _authorize_owned_write(attempt, *, preference_user=None, submission_offer=None):
    """Caller holds attempt. Publish permission under tracking only after its wait is over."""
    tracking = attempt._owned_tracking
    with tracking._lock if tracking is not None else nullcontext():
        remaining = attempt._remaining_locked()
        if attempt._stopped or attempt._decision != 'admitted' or attempt._write_possible:
            return None
        if tracking is not None:
            if submission_offer is not None:
                if submission_offer not in tracking._submissions:
                    raise ValueError('Submission intent is not retained.')
                tracking._submission_possible.add(submission_offer)
            elif preference_user is None:
                key = attempt._owned_ids[0]
                if key not in tracking._creations:
                    raise ValueError('Creation intent is not retained.')
                tracking._creation_possible.add(key)
            else:
                if preference_user not in tracking._preferences:
                    raise ValueError('Preference intent is not retained.')
                tracking._preference_possible.add(preference_user)
        attempt._write_possible = True  # Conservative before the single writer call.
        return min(8.0, remaining)


class OwnedRecordTracking:
    """One caller-owned process-local view; never infers resolution from worker completion.

    Attempt -> tracking is the only nested lock order. No tracking method calls an
    attempt while holding its own guard. No timers, retries or recovery algorithm.
    """

    def __init__(self):
        self._lock = Lock()
        self._creations = {}
        self._creation_possible = set()
        self._verified_creations = set()  # Positive origin evidence belongs only to this tracking lifetime.
        self._other_exclusions = set()
        self._preclearance_intents = {}  # Retain immutable content-free intent, never raw questions.
        self._pending_intents = {}  # Cleared immutable pending intents; never emitted in operational logs.
        self._preferences = {}
        self._preference_possible = set()
        self._submissions = {}
        self._submission_possible = set()

    def retain(self, intent):
        attempt, key = intent.attempt, str(intent.header.conversation_id)
        with attempt._lock:
            with self._lock:
                if key in self._creations or attempt._owned_tracking is not None:
                    raise ValueError('Creation intent cannot be reused.')
                self._creations[key] = intent  # Strong retention precedes any writer entry.
                attempt._owned_tracking, attempt._owned_ids = self, (key,)
                attempt._owned_selectors = IdentityReadSelectors(
                    request_id=intent.request_id, user_id=intent.header.owner_user_id)

    def creation_started(self, key):
        with self._lock:
            if key not in self._creations:
                raise ValueError('Creation intent is not retained.')
            self._creation_possible.add(key)

    def creation_verified(self, key):
        with self._lock:
            if key not in self._creations:
                raise ValueError('Creation intent is not retained.')
            self._verified_creations.add(key)  # Only the exact intended-header success path publishes it.
            self._creation_possible.discard(key)  # Exact verification clears only this reason.

    def question_origin_error(self, user_id, conversation_id):
        key = str(UUID(conversation_id))
        with self._lock:
            if self._excludes_locked((key,)):
                return 'conversation_unresolved'  # One guarded view preserves known-hold precedence.
            if (key not in self._verified_creations
                    or self._creations[key].header.owner_user_id != user_id):
                return 'conversation_read_only'
            return None  # Ordinary header reads never grant positive origin.

    def exclude_conversation(self, conversation_id):
        key = str(UUID(conversation_id))
        with self._lock:
            self._other_exclusions.add(key)  # No automatic resolution or generic recovery API.

    def retain_preclearance(self, intent):
        with self._lock:
            key = intent.marker.request_id
            if key in self._preclearance_intents:
                raise ValueError('Pre-clearance write intent cannot be reused.')
            self._preclearance_intents[key] = intent  # Strong retention before writer entry.

    def retain_pending(self, pending):
        with self._lock:
            if pending.request_id in self._pending_intents:
                raise ValueError('Pending write intent cannot be reused.')
            self._pending_intents[pending.request_id] = pending

    def pending_intent(self, request_id):
        """The retained immutable pending intent for one request, or None (Slice 5 terminal binding)."""
        with self._lock:
            return self._pending_intents.get(str(UUID(request_id)))

    def excludes(self, ids):
        keys = tuple(str(UUID(value)) for value in ids)
        with self._lock:
            return self._excludes_locked(keys)

    def _excludes_locked(self, keys):
        return any(key in self._creation_possible or key in self._other_exclusions for key in keys)

    def eligible_headers(self, headers):
        with self._lock:
            return tuple(row for row in headers if str(row.conversation_id) not in
                         self._creation_possible | self._other_exclusions)

    def retain_preference(self, intent):
        attempt, key = intent.attempt, intent.preference.owner_user_id
        with attempt._lock:
            with self._lock:
                if key in self._preference_possible or attempt._owned_tracking is not None:
                    raise ValueError('Preference update is unresolved.')
                self._preferences[key] = intent
                attempt._owned_tracking = self
                attempt._owned_selectors = IdentityReadSelectors(request_id=intent.request_id,
                                                                 user_id=key)

    def preference_unresolved(self, user_id):
        with self._lock:
            return user_id in self._preference_possible

    def preference_started(self, user_id):
        with self._lock:
            if user_id not in self._preferences:
                raise ValueError('Preference intent is not retained.')
            self._preference_possible.add(user_id)

    def preference_verified(self, user_id):
        with self._lock:
            self._preference_possible.discard(user_id)

    # Slice 6 handoff submissions: a possibly started write holds only its offer (LLD §4).
    def retain_submission(self, intent):
        attempt, key = intent.attempt, str(intent.submission.offer_id)
        with attempt._lock:
            with self._lock:
                if key in self._submission_possible or attempt._owned_tracking is not None:
                    raise ValueError('Handoff submission is unresolved.')
                self._submissions[key] = intent  # Strong retention precedes any writer entry.
                attempt._owned_tracking = self
                attempt._owned_ids = (str(UUID(intent.submission.conversation_id)),)
                attempt._owned_selectors = IdentityReadSelectors(request_id=intent.request_id,
                                                                 user_id=intent.submission.owner_user_id)

    def submission_unresolved(self, offer_id):
        with self._lock:
            return str(UUID(offer_id)) in self._submission_possible

    def submission_verified(self, offer_id):
        with self._lock:
            self._submission_possible.discard(str(UUID(offer_id)))  # Only exact read-back clears it.


class ConversationListAttempt(IdentityReadAttempt):
    def try_admit(self, allowances):
        return self._try_admit_read(allowances, Allowance.HISTORY)


def _prepare_conversation_list(selectors, *, readers, tracking, allowances, clock):
    captured = IdentityReadSelectors.model_validate(
        selectors.model_dump(warnings='error') if isinstance(selectors, IdentityReadSelectors) else selectors)
    attempt = ConversationListAttempt(clock(), clock=clock, selectors=captured)
    def run(lease):
        try:
            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            profile = verify_profile(readers.profile(captured.user_id, timeout=timeout), user_id=captured.user_id)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if profile.record is None:
                return attempt.outcome('profile_unavailable' if profile.status == 'absent' else 'unresolved')
            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            checked = verify_conversation_headers(readers.headers(captured.user_id, timeout=timeout),
                                                   user_id=captured.user_id)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if checked.record is None:
                return attempt.outcome("unresolved")
            eligible = tracking.eligible_headers(checked.record)  # Validate all rows before filtering.
            response = ConversationListResponse(request_id=captured.request_id, user_id=captured.user_id,
                conversations=tuple(ConversationReference(conversation_id=str(row.conversation_id),
                                    created_at=row.created_at.isoformat()) for row in eligible))
            response = ConversationListResponse.model_validate(response.model_dump(warnings='error'))
            attempt.bind_owned(tracking, captured,
                               (row.conversation_id for row in response.conversations))
            if tracking.excludes(attempt._owned_ids):
                return attempt.outcome('conversation_unresolved')
            decision = attempt.try_admit(allowances)
            status = {HistoryDecision.ADMITTED:'ready', HistoryDecision.REJECTED:'quota_exhausted',
                      HistoryDecision.STOPPED:'stopped'}.get(decision, 'unresolved')
            return attempt.outcome(status, response)
        except Exception:
            attempt._stop_after_failure()
            return attempt.outcome('unresolved')
    return attempt, run


def submit_conversation_list(selectors, *, slots, executor, **options):
    attempt, run = _prepare_conversation_list(selectors, **options)
    try:
        lease = slots.submit_with_lease(executor, run)
    except BaseException:
        attempt.withdraw()
        raise
    return IdentityReadHandle(lease, attempt)


async def submit_conversation_list_async(selectors, *, slots, executor, on_worker_finished=None, **options):
    attempt, run = _prepare_conversation_list(selectors, **options)
    return await _submit_owned_async(attempt, run, slots, executor, IdentityReadHandle, on_worker_finished)


@dataclass(frozen=True)
class PreferenceUpdateIntent:
    request_id: str
    preference: PreferenceRecord = field(repr=False)
    attempt: object = field(repr=False)
    lease: ExecutionLease = field(repr=False)


class PreferenceUpdateAttempt(CreationAttempt):
    """Same write budget and atomic WRITE hit; preference intent has no conversation ID."""
    def __init__(self, started_at, *, clock, user_id):
        super().__init__(started_at, clock=clock)
        self._user_id = user_id

    def authorize_write(self):
        with self._lock:
            return _authorize_owned_write(self, preference_user=self._user_id)

    def record_verified(self):
        with self._lock:
            self._verified = True
            self._owned_tracking.preference_verified(self._user_id)

    def outcome(self, status, response=None):
        with self._lock:
            if self._remaining_locked() <= 0 or self._withdrawn:
                status = 'stopped'
            if status == 'ready' and (self._stopped or not self._verified):
                status = 'unresolved'
            if status == "ready":
                self._owned_response = response
            return IdentityReadResult(status, self._charged, response if status == 'ready' else None)


def _prepare_preference_update(selectors, *, storage, tracking, reservations, allowances,
                               clock, utc_now):
    captured = PreferenceUpdateSelectors.model_validate(
        selectors.model_dump(warnings='error') if isinstance(selectors, PreferenceUpdateSelectors) else selectors)
    attempt = PreferenceUpdateAttempt(clock(), clock=clock, user_id=captured.user_id)
    def run(lease):
        try:
            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            checked = verify_profile(storage.profile(captured.user_id, timeout=timeout), user_id=captured.user_id)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if checked.record is None:
                return attempt.outcome('profile_unavailable' if checked.status == 'absent' else 'unresolved')
            try:
                reservations.reserve(lease, ("user", captured.user_id))
            except ReservationConflict:
                return attempt.outcome("preference_busy")
            if tracking.preference_unresolved(captured.user_id):
                return attempt.outcome("preference_busy")
            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            current = verify_preference(storage.preference(captured.user_id, timeout=timeout),
                                         user_id=captured.user_id)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            if current.record is None and current.status != "absent":
                return attempt.outcome("unresolved")
            intended = PreferenceRecord(record_type='preference', owner_user_id=captured.user_id,
                key=captured.key, value=captured.value, version=1 if current.record is None else current.record.version+1,
                updated_at=utc_now(), complete=True)
            decision = attempt.try_admit(allowances)
            if decision != "admitted":
                return attempt.outcome(decision)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            tracking.retain_preference(PreferenceUpdateIntent(captured.request_id, intended, attempt, lease))
            writing = PreferenceRecord.model_validate(intended.model_dump(warnings='error'))
            timeout = attempt.authorize_write()
            if timeout is None:
                return attempt.outcome("stopped")
            storage.write_preference(writing, timeout=timeout)  # Exactly once; ACK proves no save.
            timeout = attempt.step_timeout()
            if timeout is None:
                return attempt.outcome("stopped")
            native = storage.preference(captured.user_id, timeout=timeout)
            if attempt.step_timeout() is None:
                return attempt.outcome("stopped")
            checked = verify_updated_preference(native, expected=intended)
            if checked.record is None:
                return attempt.outcome("unresolved")
            attempt.record_verified()
            response = PreferenceUpdateResponse(request_id=captured.request_id, user_id=captured.user_id,
                                                  key=intended.key, value=intended.value)
            return attempt.outcome('ready', response)
        except Exception:
            return attempt.outcome('unresolved')
    return attempt, run


def submit_preference_update(selectors, *, slots, executor, clock=monotonic,
                             utc_now=lambda:datetime.now(timezone.utc), **options):
    attempt, run = _prepare_preference_update(selectors, clock=clock, utc_now=utc_now, **options)
    try:
        lease = slots.submit_with_lease(executor, run)
    except BaseException:
        attempt.withdraw()
        raise
    return IdentityReadHandle(lease, attempt)


async def submit_preference_update_async(selectors, *, slots, executor, clock=monotonic,
        utc_now=lambda:datetime.now(timezone.utc), on_worker_finished=None, **options):
    attempt, run = _prepare_preference_update(selectors, clock=clock, utc_now=utc_now, **options)
    return await _submit_owned_async(attempt, run, slots, executor, IdentityReadHandle, on_worker_finished)


async def _submit_owned_async(attempt, run, slots, executor, handle_type, observer):
    """Small integration of the existing native lease with off-loop failure withdrawal."""
    def observed(lease):
        result = None
        try:
            result = run(lease)
            return result
        finally:
            if observer is not None:
                try:
                    observer(result)
                except Exception:
                    pass

    try:
        lease = slots.submit_with_lease(executor, observed)
    except AdmissionBusy:
        raise
    except BaseException:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(attempt.withdraw, abandon_on_cancel=False)
        raise
    return handle_type(lease, attempt)



# Slice 6: simulated handoff submission (LLD §§2, 4; MLD §13). Same owned-write lifecycle as a
# preference update, with a 20-second budget and an offer hold for any unverified write.

@dataclass(frozen=True)
class HandoffIntent:
    request_id: str
    submission: object = field(repr=False)  # records.HandoffSubmission
    attempt: object = field(repr=False)
    lease: ExecutionLease = field(repr=False)


class HandoffAttempt(CreationAttempt):
    """WRITE allowance and guarded write state; handoff submission has its own 20 s budget (MLD §11)."""

    def __init__(self, started_at, *, clock, offer_id):
        super().__init__(started_at, clock=clock)
        self._deadline = started_at + 20.0
        self._offer_id = str(UUID(offer_id))

    def authorize_write(self):
        with self._lock:
            return _authorize_owned_write(self, submission_offer=self._offer_id)

    def record_verified(self):
        with self._lock:
            self._verified = True
            self._owned_tracking.submission_verified(self._offer_id)

    def outcome(self, status, response=None):
        with self._lock:
            if self._remaining_locked() <= 0 or self._withdrawn:
                status = 'stopped'
            if status == 'ready' and (self._stopped or not self._verified):
                status = 'unresolved'
            if status == 'ready':
                self._owned_response = response
            return IdentityReadResult(status, self._charged, response if status == 'ready' else None)


def _prepare_handoff(selectors, *, storage, tracking, reservations, allowances, clock, utc_now,
                     uuid_factory=uuid4):
    from shopunow.records import (HandoffSubmission, submission_id_for, verify_offer_link, verify_submission,
                                  verify_submittable_offer)
    from shopunow.schemas import HandoffResponse, HandoffSelectors
    captured = HandoffSelectors.model_validate(
        selectors.model_dump(warnings='error') if isinstance(selectors, HandoffSelectors) else selectors)
    attempt = HandoffAttempt(clock(), clock=clock, offer_id=captured.offer_id)
    offer_key = str(UUID(captured.offer_id))

    def read(call, *args):
        timeout = attempt.step_timeout()
        if timeout is None:
            raise _Stopped()
        value = call(*args, timeout=timeout)
        if attempt.step_timeout() is None:
            raise _Stopped()
        return value

    def receipt(record):
        return HandoffResponse(request_id=captured.request_id, user_id=captured.user_id,
            conversation_id=captured.conversation_id, submission_id=record.submission_id,
            receipt_id=record.receipt_id, contact_method='email' if record.email is not None else 'phone',
            simulated=True)

    def run(lease):
        try:
            profile = verify_profile(read(storage.profile, captured.user_id), user_id=captured.user_id)
            if profile.record is None:
                return attempt.outcome('profile_unavailable' if profile.status == 'absent' else 'unresolved')
            offer = verify_submittable_offer(read(storage.exact_offer, offer_key), user_id=captured.user_id,
                conversation_id=captured.conversation_id, offer_id=offer_key)
            if offer.record is None:
                # Absent, foreign and other-conversation offers share one safe failure.
                return attempt.outcome('unresolved' if offer.status == 'unresolved' else 'offer_unavailable')
            offer = offer.record
            terminal = read(storage.exact_terminal, offer.request_id)
            marker = None if terminal.get('ids') else read(storage.exact_preclearance_marker, offer.request_id)
            if not verify_offer_link(terminal, marker, offer=offer):
                return attempt.outcome('offer_unavailable')  # Unlinked offers are unusable (LLD §4).
            try:
                reservations.reserve(lease, ('offer', offer_key))
                reservations.reserve(lease, ('conversation', str(UUID(captured.conversation_id))))
            except ReservationConflict:
                return attempt.outcome('handoff_busy')
            if tracking.submission_unresolved(offer_key):
                return attempt.outcome('handoff_busy')  # An earlier write may still commit.
            if tracking.excludes((captured.conversation_id,)):
                return attempt.outcome('conversation_unresolved')  # Before any charge or write (review S1).
            decision = attempt.try_admit(allowances)  # After ownership checks (LLD §2).
            if decision != 'admitted':
                return attempt.outcome(decision)
            existing = verify_submission(read(storage.exact_submission, offer_key), offer=offer)
            form = (captured.name, captured.phone, captured.email)
            if existing.record is not None:
                if (existing.record.name, existing.record.phone, existing.record.email) != form:
                    return attempt.outcome('handoff_conflict')  # Never overwrite a saved submission.
                with attempt._lock:
                    attempt._verified = True  # The saved identical record is the exact read-back.
                tracking.retain_submission(HandoffIntent(captured.request_id, existing.record, attempt, lease))
                return attempt.outcome('ready', receipt(existing.record))
            if existing.status != 'absent':
                return attempt.outcome('unresolved')
            intended = HandoffSubmission(record_type='submission',
                submission_id=submission_id_for(offer_key), receipt_id=str(uuid_factory()),
                owner_user_id=captured.user_id, conversation_id=str(UUID(captured.conversation_id)),
                offer_id=offer_key, request_reference=offer.request_id, reason_code=offer.reason_code,
                target=offer.target_department_or_fallback, name=captured.name, phone=captured.phone,
                email=captured.email, simulated_review=True, complete=True, created_at=utc_now())
            left = attempt.step_timeout()
            if left is None or left < SUBMISSION_WRITE_FLOOR:
                return attempt.outcome('stopped')  # Too little time for write + read-back: no hold (review M1).
            tracking.retain_submission(HandoffIntent(captured.request_id, intended, attempt, lease))
            writing = HandoffSubmission.model_validate_json(intended.model_dump_json(warnings='error'))
            timeout = attempt.authorize_write()
            if timeout is None:
                return attempt.outcome('stopped')
            try:
                storage.write_submission(writing, timeout=timeout)  # Exactly once; acknowledgement proves nothing.
            except Exception:
                pass  # Outcome unknown: only the exact read-back below may decide.
            # Required cleanup (challenger D1/D2): one bounded exact read-back even after a deadline or
            # withdrawal, so a saved record releases its hold. It never turns a stopped request into a
            # receipt: outcome() still reports 'stopped'. Anything else keeps the offer held, no retry.
            try:
                checked = verify_submission(storage.exact_submission(offer_key, timeout=8.0), offer=offer)
            except Exception:
                return attempt.outcome('unresolved')
            if checked.record != intended:
                return attempt.outcome('unresolved')
            attempt.record_verified()
            return attempt.outcome('ready', receipt(intended))
        except _Stopped:
            return attempt.outcome('stopped')
        except Exception:
            return attempt.outcome('unresolved')
    return attempt, run


class _Stopped(Exception):
    """Internal: the handoff budget expired or the request was withdrawn between steps."""


SUBMISSION_WRITE_FLOOR = 4.0  # Provisional: least time left to start the write and still read it back.


def submit_handoff(selectors, *, slots, executor, clock=monotonic,
                   utc_now=lambda: datetime.now(timezone.utc), **options):
    attempt, run = _prepare_handoff(selectors, clock=clock, utc_now=utc_now, **options)
    try:
        lease = slots.submit_with_lease(executor, run)
    except BaseException:
        attempt.withdraw()
        raise
    return IdentityReadHandle(lease, attempt)


async def submit_handoff_async(selectors, *, slots, executor, clock=monotonic,
                               utc_now=lambda: datetime.now(timezone.utc), on_worker_finished=None, **options):
    attempt, run = _prepare_handoff(selectors, clock=clock, utc_now=utc_now, **options)
    return await _submit_owned_async(attempt, run, slots, executor, IdentityReadHandle, on_worker_finished)


# Handoff flow: slot -> profile -> owned exact offer -> verified link -> offer+conversation reservation
# -> WRITE hit -> exact existing submission (same receipt | 409 | absent) -> retain -> one write
# -> exact read-back -> receipt; an unverified write holds the offer with no retry.
