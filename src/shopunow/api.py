# FastAPI v0.143.2 / Starlette v1.8.0 / Pydantic v2.14.1
# Explicit input configuration prevents imports from loading credentials or clients.
import hmac  # Compare credential bytes without an ordinary early-exit comparison.
import json  # Serialize only the application's allowlisted event fields.
import logging  # Emit only fixed codes and opaque request references.
import anyio  # Own shielded attempt-control thread calls and cooperative waits.
from concurrent.futures import CancelledError, TimeoutError  # Distinguish lease outcomes.
from collections.abc import Awaitable, Callable  # Describe the dependency signature.
from datetime import datetime, timezone  # Record explicit UTC time independently of sinks.
from typing import Annotated  # Express ordered native dependency prerequisites.
from uuid import UUID, uuid4  # Generate references independently of submitted content.

from fastapi import Depends, FastAPI, Query, Request  # Native dependency/query interfaces.
from fastapi.exceptions import RequestValidationError  # Sanitize framework errors.
from pydantic import ValidationError  # Catch schema/JSON rejection without echoing it.
from starlette.exceptions import HTTPException  # Cover framework 404/405 errors too.
from starlette.responses import JSONResponse  # Produce the safe error envelope.

from shopunow.schemas import (  # Native input checks and allowlisted outward models.
    HistoryAssistantMessage, HistoryNotice, HistoryResponse, HistorySelectors,
    HistoryUserMessage, HistoryQuery, QuestionRequest,
)
from shopunow.schemas import HealthResponse  # Static liveness has no resource/allowance dependency.
from shopunow.records import CompleteTranscript  # Existing verified internal result only.
from shopunow.admission import (  # Existing completion owners and guarded controls.
    QuestionAdmissionHandle, QuestionAdmissionResult, HistoryAdmissionHandle,
)


from shopunow.schemas import (
    ProfileResolveRequest, PreferenceReadQuery,
    ProfileResolveResponse, PreferenceReadResponse, ProfileBinding,
)
from shopunow.schemas import (ConversationCreateResponse)
from shopunow.schemas import InteractionResponse as InteractionResponse
# Explicit re-exports preserve the schema names used by injected HTTP hosts.
from shopunow.schemas import (
    IdentityReadSelectors as IdentityReadSelectors,
    ConversationCreateRequest as ConversationCreateRequest,
    ConversationListResponse as ConversationListResponse,
    PreferenceUpdateRequest as PreferenceUpdateRequest,
    PreferenceUpdateResponse as PreferenceUpdateResponse,
    PreferenceUpdateSelectors as PreferenceUpdateSelectors,
)

logger = logging.getLogger("shopunow.entry")  # Sink configuration belongs to the caller.
ERRORS = {
    401: ("unauthorized", "The API token is invalid."),
    404: ("not_found", "The operation was not found."),
    405: ("method_not_allowed", "The method is not allowed."),
    413: ("body_too_large", "The request body is too large."),
    422: ("invalid_request", "The request is invalid."),
}  # Exception details never become user-facing messages.


def entry_event(reference: str, outcome: str, status: int | None = None) -> None:
    """Emit the small event available here; no workflow/admission claim is made."""
    event = {
        "utc_time": datetime.now(timezone.utc).isoformat(),
        "event": "entry_validation",
        "request_id": reference,
        "outcome": outcome,
    }  # Every argument comes from application-generated IDs and fixed codes.
    if status is not None:
        event["http_status"] = status
    try:
        logger.info(json.dumps(event))  # Never serialize requests or exceptions.
    except Exception:
        pass  # A broken sink cannot turn a rejected request into an approved one.


def request_reference(request: Request) -> str:
    """Reuse one opaque reference for validation and its safe response/log event."""
    if not hasattr(request.state, "request_id"):
        request.state.request_id = uuid4().hex  # Never derive IDs from user input.
    return request.state.request_id


def safe_error(request: Request, status: int) -> JSONResponse:
    """Convert a controlled status to content-free public and operational output."""
    code, message = ERRORS.get(status, ("technical_error", "The operation failed."))
    reference = request_reference(request)  # Correlate without logging selectors.
    entry_event(reference, code, status)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": reference}},
    )


def create_app(*, lifespan=None) -> FastAPI:
    """Build only the safe error boundary; no interaction or provider is registered."""
    # Default startup still performs no external initialization. The host may set OpenTelemetry
    # auto-export variables; this app approves no telemetry export, so FastAPI must not configure one.
    application = FastAPI(lifespan=lifespan, telemetry={'auto_configure': False})

    @application.get('/health',response_model=HealthResponse)
    async def health():
        return HealthResponse()  # Public liveness: no token, profile, slot or quota lookup.


    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return safe_error(request, error.status_code)  # Discard raw exception details.

    @application.exception_handler(RequestValidationError)
    async def shape_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return safe_error(request, 422)  # Do not serialize error.body or error.errors().

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        # Native server-error handling fixes the response, not server traceback sinks.
        return safe_error(request, 500)

    return application


def question_validator(
    *, api_token: str, body_limit: int = 32768
) -> Callable[[Request], Awaitable[QuestionRequest]]:
    """Build token -> bounded body -> schema checks, without admission or work.

    32 KiB admits compact standard JSON encodings of a 2,000-character question,
    including 24,000 escaped astral bytes plus bounded selectors. Arbitrarily
    padded JSON remains subject to the independent configurable byte cap.
    """
    return _json_request_validator(api_token=api_token, body_limit=body_limit,
                                   request_model=QuestionRequest)


def _json_request_validator(*, api_token, body_limit, request_model):
    """Same-package token/byte/JSON checks for fixed application request models."""
    if not api_token or not api_token.isascii():
        raise ValueError("An explicit nonempty ASCII API token is required.")
    if type(body_limit) is not int or body_limit <= 0:
        raise ValueError("The body limit must be a positive integer.")
    expected_token = api_token.encode("ascii")  # Use only explicitly supplied config.

    async def validate(request: Request) -> QuestionRequest:
        request_reference(request)  # Prepare correlation before any rejection.
        supplied_tokens = request.headers.getlist("x-api-token")
        # Reject missing/duplicate/non-ASCII tokens before consuming body bytes.
        if (
            len(supplied_tokens) != 1
            or not supplied_tokens[0].isascii()
            or not hmac.compare_digest(supplied_tokens[0].encode("ascii"), expected_token)
        ):
            raise HTTPException(401)

        body = bytearray()  # Bound retained data using actual received bytes.
        async for chunk in request.stream():
            if len(body) + len(chunk) > body_limit:
                raise HTTPException(413)  # Never parse an oversized body.
            body.extend(chunk)  # Content-Length cannot bypass the actual-byte cap.

        if request.query_params:
            raise HTTPException(422)  # Controls belong only in this operation's body.
        try:
            text = body.decode("utf-8")  # Reject invalid wire encoding first.
            payload = request_model.model_validate_json(text)  # Public native API.
        except (UnicodeError, ValidationError):
            raise HTTPException(422) from None  # Suppress content-bearing diagnostics.

        entry_event(request_reference(request), "validated")
        return payload  # No trimming, quota, storage, retrieval, or model calls.

    return validate


def history_validator(*, api_token: str) -> Callable[..., Awaitable[HistoryQuery]]:
    """Build token -> bounded empty body -> unique native query model validation.

    The caller injects the returned dependency with Depends; the normal app factory
    registers no history route. Validated syntax grants no ownership or admission.
    """
    return _read_query_validator(api_token=api_token, query_model=HistoryQuery)


def _read_query_validator(*, api_token, query_model):
    """Use native query models after token, empty-body and duplicate checks."""
    if not api_token or not api_token.isascii():
        raise ValueError("An explicit nonempty ASCII API token is required.")
    expected_token = api_token.encode("ascii")  # No environment or credential lookup.

    async def validate_token(request: Request) -> None:
        request_reference(request)  # Correlate every safe rejection independently of input.
        supplied = request.headers.getlist("x-api-token")
        if (
            len(supplied) != 1
            or not supplied[0].isascii()
            or not hmac.compare_digest(supplied[0].encode("ascii"), expected_token)
        ):
            raise HTTPException(401)  # Reject before the first receive/body read.

    async def validate_body(
        request: Request, token_checked: Annotated[None, Depends(validate_token)],
    ) -> None:
        body_bytes = 0  # Count actual bytes without retaining any body content.
        async for chunk in request.stream():
            body_bytes += len(chunk)
            if body_bytes > 32768:
                raise HTTPException(413)  # Oversize takes precedence over nonempty/query.
        if body_bytes:
            raise HTTPException(422)  # GET accepts no body, including whitespace or JSON.

    async def validate_multiplicity(
        request: Request, body_checked: Annotated[None, Depends(validate_body)],
    ) -> None:
        for key in request.query_params:
            if len(request.query_params.getlist(key)) != 1:
                raise HTTPException(422)  # Do not lose duplicates during scalar extraction.

    async def validate(
        request: Request,
        payload: Annotated[query_model, Query()],
        unique_query: Annotated[None, Depends(validate_multiplicity)],
    ) -> HistoryQuery:
        # FastAPI resolves prerequisites before its native query-model field validation.
        entry_event(request_reference(request), "validated")
        return payload  # No storage, execution slot, allowance or history success claim.

    return validate


# History flow: explicit token -> actual byte cap -> empty body -> unique strict query.


QUESTION_ERRORS = {
    "conversation_read_only": (409, "Start a new conversation to ask a question."),
    "execution_capacity": (503, "Execution capacity is unavailable."),
    "profile_unavailable": (404, "The profile is unavailable."),
    "ownership_failure": (404, "The conversation is unavailable."),
    "conversation_busy": (409, "The conversation is busy."),
    "conversation_unresolved": (409, "The conversation state could not be verified."),
    "conversation_full": (409, "This conversation is full; start a new conversation."),
    "question_limit": (429, "The question allowance is exhausted."),
    "interaction_not_ready": (503, "The interaction is not available in this local test."),
    "request_timeout": (503, "The request timed out."),
    "request_stopped": (503, "The request stopped."),
    "technical_error": (500, "The operation failed."),
}  # Fixed public vocabulary; no private exception, quota or stored content is serialized.


def question_event(reference: str, outcome: str, status: int | None = None,
                   *, stage: str = "question_terminal") -> None:
    """Emit one allowlisted question transition using the existing application sink."""
    event = {"utc_time": datetime.now(timezone.utc).isoformat(), "event": stage,
             "request_id": reference, "outcome": outcome}
    if status is not None:
        event["http_status"] = status
    try:
        logger.info(json.dumps(event))
    except Exception:
        pass  # Logging never grants work permission or changes the selected response.


def question_error(request: Request, code: str) -> JSONResponse:
    """Select a fixed safe envelope without implying delivery or a refund."""
    status, message = QUESTION_ERRORS[code]
    reference = request_reference(request)
    question_event(reference, code, status)
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message,
                                           "request_id": reference}})


async def stop_question_admission(handle: QuestionAdmissionHandle) -> None:
    """Record stop and request native pre-start cancellation, never join running work."""
    with anyio.CancelScope(shield=True):  # The shield includes the native limiter wait.
        await anyio.to_thread.run_sync(handle.cancel, abandon_on_cancel=False)


def observe_question_admission(request: Request, handle: QuestionAdmissionHandle) -> None:
    """Track native lease completion independently of visible HTTP task lifetime."""
    reference = request_reference(request)

    def completed(lease):
        try:
            result = lease.result(0)
            outcome = result.status
        except CancelledError:
            outcome = "cancelled_before_start"
        except BaseException:
            outcome = "technical_error"  # Never emit exception diagnostics.
        question_event(reference, outcome, stage="question_completion")
        if lease.capacity_released:
            question_event(reference, "released", stage="question_capacity_released")

    handle.lease.add_done_callback(completed)  # Same native completion owner.


def question_worker_observer(request: Request) -> Callable[[QuestionAdmissionResult | None], None]:
    """Retain only an opaque reference even if dispatch fails before returning a handle."""
    reference = request_reference(request)

    def worker_finished(result):
        outcome = "technical_error" if result is None else result.status
        decision = (result.quota_decision.value if result is not None and
                    result.quota_decision is not None else outcome)
        question_event(reference, decision, stage="question_admission")
        question_event(reference, outcome, stage="question_worker_finished")
        # The native Future settles after this callback returns: no release claim here.

    return worker_finished


async def question_admission_response(request: Request,
                                      handle: QuestionAdmissionHandle):
    """Wait by the current phase, rechecking under the attempt guard before selection."""
    result = None
    while True:
        def inspect():
            nonlocal result
            try:
                result = handle.lease.result(0)  # Never wait for business completion.
            except TimeoutError:
                pass
            except CancelledError:
                return None  # Confirmed native pre-start cancellation has no normal result.
            return handle.attempt.http_state(result)  # Potential guard wait stays off the loop.

        with anyio.CancelScope(shield=True):
            state = await anyio.to_thread.run_sync(inspect, abandon_on_cancel=False)
        await anyio.lowlevel.checkpoint()  # Observe owner cancellation before selecting a response.
        if state is None:
            return question_error(request, "request_stopped")
        if state.code is not None:
            await stop_question_admission(handle)
            return question_error(request, state.code)
        if result is not None:
            if result.status == 'admitted' and result.interaction is not None:
                if UUID(result.interaction.request_id) != UUID(request_reference(request)):
                    return question_error(request, 'technical_error')
                return result.interaction  # Native FastAPI response_model validates and serializes it.
            code = {"admitted": "interaction_not_ready", "quota_exhausted": "question_limit",
                    "stopped": "request_stopped", "unresolved": "technical_error"}.get(
                        result.status, result.status)
            return question_error(request, code)
        try:
            await handle.lease.wait(state.remaining)  # Caller-only wait; worker ownership is unchanged.
        except (TimeoutError, CancelledError):
            pass  # Recheck under the guard: an eligible hit may have established the next phase.


async def question_response_state(handle: QuestionAdmissionHandle):
    """Final original-budget check after native serialization and all guard waits."""
    def inspect():
        return handle.attempt.http_state(handle.lease.result(0))

    with anyio.CancelScope(shield=True):
        state = await anyio.to_thread.run_sync(inspect, abandon_on_cancel=False)
    await anyio.lowlevel.checkpoint()  # Observe cancellation before any outbound response starts.
    return state


# Overall flow: explicit config -> token -> bounded stream -> schema -> validated input.


def project_history_response(
    transcript: CompleteTranscript, *, selectors: HistorySelectors,
) -> HistoryResponse:
    """Format a successfully verified transcript using the same operation's selectors.

    The caller must supply the complete successful verification and captured validated
    selectors from that operation. This function performs no I/O or authorization;
    empty histories and technical-only notices retain no owner/conversation binding,
    so their ownership cannot be established here by echoing selectors. Other records'
    retained bindings are checked only to catch accidental relabelling, not to grant
    clearance, exhaustive-scan, freshness or durable-storage proof.
    """
    def require_binding(record) -> None:
        # Catch accidental selector rebinding wherever the verified result retains identity.
        if (record.owner_user_id != selectors.user_id or
                str(UUID(record.conversation_id)) != selectors.conversation_id):
            raise ValueError("The transcript does not match the captured selectors.")

    messages = []  # Detach public fields from all internal records and native arrays.
    notices = []  # Keep sort timestamps beside models, never inside the public payload.
    for item in transcript.exchanges:
        pending, terminal = item.exchange.pending, item.exchange.terminal
        require_binding(pending)
        require_binding(terminal)
        if item.offer is not None:
            require_binding(item.offer)
            notices.append((terminal.created_at, HistoryNotice(
                request_id=terminal.request_id, reason_code=item.offer.reason_code,
                offer_id=item.offer.offer_id,
                code_owned_text="A simulated support offer was saved for this exchange.",
            )))  # Historical link only: no current eligibility, receipt or delivery claim.
        messages.extend((
            HistoryUserMessage(message_id=pending.message_id, request_id=pending.request_id,
                               sequence=pending.sequence, text=pending.text),
            HistoryAssistantMessage(message_id=terminal.outcome_id, request_id=terminal.request_id,
                                    sequence=terminal.sequence, text=terminal.approved_text_or_code,
                                    outcome_kind=terminal.kind),
        ))  # Preserve original order, positions and text; no automatic field dumping.
    for notice in transcript.notices:
        if notice.offer is not None:
            require_binding(notice.offer)
        fixed_text = {
            "security_uncertain": "The request could not be completed safely.",
            "dependency_failure": "The request could not be completed.",
            "technical_error": "The operation failed.",
        }[notice.reason_code]  # Select only fixed code-owned wording, never stored model text.
        notices.append((notice.created_at, HistoryNotice(
            request_id=notice.request_id, reason_code=notice.reason_code,
            offer_id=(notice.offer.offer_id if notice.offer is not None and
                      notice.reason_code != "technical_error" else None),
            code_owned_text=fixed_text,
        )))  # Technical notices contain no offer even if a caller retains extra linkage.
    notices.sort(key=lambda item: (item[0], item[1].request_id))
    return HistoryResponse(request_id=selectors.request_id, user_id=selectors.user_id,
                           conversation_id=selectors.conversation_id,
                           messages=tuple(messages), notices=tuple(notice for _, notice in notices))


# Projection flow: same-operation verified prefix -> public fields -> native response model.


HISTORY_ERRORS = {
    **QUESTION_ERRORS,
    "history_limit": (429, "The history allowance is exhausted."),
}  # Reuse the existing safe vocabulary; history has its own named allowance.


def history_event(reference: str, outcome: str, status: int | None = None,
                  *, stage: str = "history_terminal") -> None:
    """Emit content-free history transitions using the existing safe sink."""
    question_event(reference, outcome, status, stage=stage)


def history_error(request: Request, code: str) -> JSONResponse:
    """Build a fixed safe error; never retain stored content or raw exception data."""
    status, message = HISTORY_ERRORS[code]
    reference = request_reference(request)
    history_event(reference, code, status)
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "request_id": reference},
    })


async def stop_history_admission(handle: HistoryAdmissionHandle) -> None:
    """Record withdrawal off the loop, retaining all running worker capacity."""
    with anyio.CancelScope(shield=True):
        await anyio.to_thread.run_sync(handle.cancel, abandon_on_cancel=False)


def history_worker_observer(request: Request) -> Callable:
    """Capture only the opaque reference, including dispatch failure without a handle."""
    reference = request_reference(request)

    def finished(result):
        outcome = "technical_error" if result is None else result.status
        decision = (result.quota_decision.value if result is not None and
                    result.quota_decision is not None else outcome)
        history_event(reference, decision, stage="history_admission")
        history_event(reference, outcome, stage="history_worker_finished")

    return finished  # Native completion follows this callback; no release claim is made.


def observe_history_admission(request: Request, handle: HistoryAdmissionHandle) -> None:
    """Observe the same native completion owner independently of HTTP lifetime."""
    reference = request_reference(request)

    def completed(lease):
        try:
            outcome = lease.result(0).status
        except CancelledError:
            outcome = "cancelled_before_start"
        except BaseException:
            outcome = "technical_error"
        history_event(reference, outcome, stage="history_completion")
        if lease.capacity_released:
            history_event(reference, "released", stage="history_capacity_released")

    handle.lease.add_done_callback(completed)


async def history_admission_state(handle: HistoryAdmissionHandle, result=None):
    """Sample the original deadline after any limiter/attempt-guard wait."""
    with anyio.CancelScope(shield=True):
        state = await anyio.to_thread.run_sync(
            handle.attempt.checked_state, result, abandon_on_cancel=False,
        )
    await anyio.lowlevel.checkpoint()  # Observe host cancellation before selecting output.
    return state


async def history_admission_response(request: Request, handle: HistoryAdmissionHandle):
    """Return a model for native FastAPI serialization, or a fixed controlled error.

    The HTTP owner must recheck success at response.start after native serialization.
    Worker completion alone cannot authorize delayed outbound history.
    """
    result = None
    while True:
        cancelled = False
        try:
            result = handle.lease.result(0)  # Never synchronously wait for business completion.
        except TimeoutError:
            pass
        except CancelledError:
            cancelled = True
        state = await history_admission_state(handle, result)
        if state.code is not None:
            await stop_history_admission(handle)
            return history_error(request, state.code)
        if cancelled:
            return history_error(request, "request_stopped")
        if result is not None:
            if result.status == "ready":
                return result.response  # Native response_model validates and serializes this.
            code = {"quota_exhausted": "history_limit", "stopped": "request_stopped",
                    "unresolved": "technical_error"}.get(result.status, result.status)
            return history_error(request, code)
        try:
            await handle.lease.wait(state.remaining)  # Same original deadline; no new phase.
        except (TimeoutError, CancelledError):
            pass  # Recheck stop/deadline after True, False, uncertain or delayed completion.


# HTTP flow: captured selectors -> immediate shared lease -> guarded wait -> native model;
# the owned ASGI host guards outbound success and joins observers through actual sending.


# Identity reads project only verified records; no I/O or default preference is created.


def project_identity_read(profile, preference, *, selectors, operation):
    """Build one native response from previously verified owner-bound records."""
    if profile.user_id != selectors.user_id:
        raise ValueError('The stored profile does not match the initiating selector.')
    if operation == 'profile':
        return ProfileResolveResponse(
            request_id=selectors.request_id, user_id=selectors.user_id, created=False,
            profile=ProfileBinding(user_id=profile.user_id, role=profile.role,
                                   home_department=profile.home_department))
    if operation != 'preference':
        raise ValueError('Unsupported internal read operation.')
    if preference is not None and preference.owner_user_id != selectors.user_id:
        raise ValueError('The preference does not match the initiating selector.')
    return PreferenceReadResponse(request_id=selectors.request_id, user_id=selectors.user_id,
                                  response_style=None if preference is None else preference.value)


# Reuse the installed FastAPI dependency machinery; models are selected by application code.
def profile_resolve_validator(*, api_token):
    return _json_request_validator(api_token=api_token, body_limit=32768,
                                   request_model=ProfileResolveRequest)


def preference_read_validator(*, api_token):
    return _read_query_validator(api_token=api_token, query_model=PreferenceReadQuery)


IDENTITY_ERRORS = {
    **QUESTION_ERRORS,
    'profile_read_limit': (429, 'The profile read allowance is exhausted.'),
}  # Both identity read operations consume the same category.


def identity_event(reference, outcome, status=None, *, stage='identity_terminal', elapsed_ms=None):
    event = {'utc_time': datetime.now(timezone.utc).isoformat(), 'event': stage,
             'request_id': reference, 'outcome': outcome}
    if status is not None:
        event['http_status'] = status
    if elapsed_ms is not None:
        event['elapsed_ms'] = elapsed_ms  # Measured interval, never an estimated provider duration.
    try:
        logger.info(json.dumps(event))
    except Exception:
        pass


def identity_error(request, code):
    status, message = IDENTITY_ERRORS[code]
    identity_event(request_reference(request), code, status)
    return JSONResponse(status_code=status, content={'error': {
        'code': code, 'message': message, 'request_id': request_reference(request)}})


def identity_worker_observer(request):
    from time import monotonic  # Measure submission-to-worker-return, independently of fake budgets.
    reference, started = request_reference(request), monotonic()
    def worker_finished(result):
        charge = None if result is None else result.charged
        decision = 'admitted' if charge is True else 'rejected' if charge is False else 'unresolved'
        identity_event(reference, decision, stage='identity_admission')
        identity_event(reference, 'technical_error' if result is None else result.status,
                       stage='identity_worker_finished',
                       elapsed_ms=max(0, round((monotonic()-started)*1000)))
    return worker_finished


def observe_identity_read(request, handle):
    reference = request_reference(request)
    def completed(lease):
        try:
            outcome = lease.result(0).status
        except CancelledError:
            outcome = 'cancelled_before_start'
        except BaseException:
            outcome = 'technical_error'
        identity_event(reference, outcome, stage='identity_completion')
        if lease.capacity_released:
            identity_event(reference, 'released', stage='identity_capacity_released')
    handle.lease.add_done_callback(completed)


async def identity_read_response(request, handle, *, limit_code="profile_read_limit", project_result=None):
    """Return a native model, requiring final state recheck after serialization by HTTP owner."""
    result = None
    while True:
        cancelled = False
        try:
            result = handle.lease.result(0)
        except TimeoutError:
            pass
        except CancelledError:
            cancelled = True
        state = await history_admission_state(handle, result)  # Shared off-loop read controls.
        if state.code is not None:
            await stop_history_admission(handle)
            return identity_error(request, state.code)
        if cancelled:
            return identity_error(request, 'request_stopped')
        if result is not None:
            if result.status == 'ready':
                if project_result is None:
                    return result.response  # Native validation/serialization follows in FastAPI.
                response = project_result(result)
                # Creation projection binds under the attempt guard off the event loop.
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(handle.attempt.bind_response, response,
                                                   abandon_on_cancel=False)
                return response
            code = {'quota_exhausted': limit_code, 'stopped': 'request_stopped',
                    'unresolved': 'technical_error'}.get(result.status, result.status)
            return identity_error(request, code)
        try:
            await handle.lease.wait(state.remaining)
        except (TimeoutError, CancelledError):
            pass


# Flow: native dependency -> one owned worker -> native response -> final guarded HTTP check.



IDENTITY_ERRORS.update({
    'creation_busy': (409, 'The conversation creation is busy.'),
    'preference_busy': (409, 'The preference update is busy or unresolved.'),
    'write_limit': (429, 'The write allowance is exhausted.'),
    'offer_unavailable': (404, 'The support offer is unavailable.'),
    'handoff_busy': (409, 'The support request is busy or could not be verified yet.'),
    'handoff_conflict': (409, 'Different details were already submitted for this support offer.'),
    'history_limit': HISTORY_ERRORS['history_limit'],
})  # All protected record operations retain the one safe envelope and fixed vocabulary.


def project_creation_response(result):
    return ConversationCreateResponse(request_id=result.request_id, user_id=result.user_id,
                                       conversation_id=result.conversation_id)


async def owned_record_response(request, handle, *, operation):
    """Native response path with the existing current-state check and completion ownership."""
    return await identity_read_response(request, handle,
        limit_code='history_limit' if operation == 'list' else 'write_limit',
        project_result=project_creation_response if operation == 'create' else None)
