# FastAPI v0.143.2 / AnyIO v4.15.1; question route host (moved from tests in Slice 6; unchanged behaviour).
import anyio  # Own the application and receive observer through their entire lifetimes.
from typing import Annotated  # Keep validation in FastAPI's native dependency lifecycle.
from fastapi import Depends, Request  # Use public request and dependency interfaces.

from shopunow import admission, api  # Compose the actual production boundary components.


async def run_owned_application(application, scope, receive, send, *, state, stop_admission):
    """Run one ASGI call in an owned task group; shared by the question host and the storage (history) host.

    The application runs as a child task, so the server-facing task is never it. Its cleanup stops admission under
    a shield before any await, and its first exception is re-raised only after the group has joined.
    """
    finished = anyio.Event()
    errors = []  # Preserve native application exceptions after the owned group joins.

    async def run_application():
        try:
            with anyio.CancelScope() as application_scope:
                state["application_scope"] = application_scope
                try:
                    await application(scope, receive, send)
                finally:
                    # Shield before the first await, including thread-token acquisition.
                    with anyio.CancelScope(shield=True):
                        state["cleanup_started"].set()
                        if state["handle"] is not None:
                            await stop_admission(state["handle"])
        except Exception as error:
            errors.append(error)  # Safe FastAPI handler retains its normal re-raise behavior.
        finally:
            state["application_done"] = True
            finished.set()

    async with anyio.create_task_group() as group:
        state["group"] = group
        group.start_soon(run_application)  # The server-facing task never is this child.
        try:
            await finished.wait()
        finally:
            group.cancel_scope.cancel()  # Native group exit joins observer and child cleanup.
    if errors:
        raise errors[0]


class QuestionHTTPApp:
    """ASGI host for POST /interactions; the storage app serves it only when answering is configured."""

    def __init__(self, *, api_token, body_limit=32768, record_states=False, **admission_options):
        self.application = api.create_app()
        self.states = [] if record_states else None  # Test-only observations; unbounded if served.
        self.request_started = anyio.Event()
        validate = api.question_validator(api_token=api_token, body_limit=body_limit)
        continuation = admission_options.pop('on_admitted', None)
        self.before_response_start = admission_options.pop('before_response_start', None)

        @self.application.exception_handler(Exception)
        async def unexpected_error(request, error):
            handle = request.scope['question_http']['handle']
            if handle is None:
                return api.safe_error(request, 500)
            current = await api.question_response_state(handle)
            return api.question_error(request, current.code or 'technical_error')

        @self.application.post("/interactions", response_model=api.InteractionResponse)
        async def question(request: Request, payload: Annotated[object, Depends(validate)]):
            # The dependency finishes the single body reader before this endpoint starts.
            state = request.scope["question_http"]
            try:
                handle = await admission.submit_question_admission_async(
                    payload, on_worker_finished=api.question_worker_observer(request),
                    on_admitted=(None if continuation is None else
                        lambda **state: continuation(request_id=api.request_reference(request), **state)),
                    **admission_options,
                )
            except admission.AdmissionBusy:
                return api.question_error(request, "execution_capacity")
            state["handle"] = handle  # Successful submission has no suspension before assignment.
            state["submitted"].set()
            api.observe_question_admission(request, handle)
            state["group"].start_soon(observe_disconnect, request, state)
            return await api.question_admission_response(request, handle)

        async def observe_disconnect(request, state):
            state["observer_started"] = True
            state["observer_ready"].set()
            try:
                while True:
                    message = await request.receive()  # Only listener after validated body.
                    if message["type"] == "http.disconnect":
                        state["closed"] = True
                        await api.stop_question_admission(state["handle"])
                        state["application_scope"].cancel()
                        return
            finally:
                state["observer_done"] = True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.application(scope, receive, send)
        state = {"handle": None, "closed": False, "observer_started": False,
                 "observer_done": False, "application_done": False,
                 "submitted": anyio.Event(), "observer_ready": anyio.Event(),
                 "cleanup_started": anyio.Event(), "response_started": False,
                 "success_suppressed": False}
        if self.states is not None:
            self.states.append(state)
        self.request_started.set()
        scope["question_http"] = state

        async def receive_once():
            message = await receive()
            if message["type"] == "http.disconnect":
                state["closed"] = True  # Also cover disconnect during incomplete body input.
            return message

        async def send_if_open(message):
            await anyio.lowlevel.checkpoint()  # Owner cancellation forbids a fresh outbound send.
            if state["closed"] or state['success_suppressed']:
                return  # No second response, or delivery claim, after known closure.
            if (message['type'] == 'http.response.start' and message['status'] == 200
                    and state['handle'] is not None):
                if self.before_response_start is not None:
                    await self.before_response_start(state)  # Controlled post-serialization boundary.
                current = await api.question_response_state(state['handle'])
                if current.code is not None:
                    state['success_suppressed'] = True
                    replacement = api.question_error(Request(scope), current.code)
                    await replacement(scope, receive_once, send_actual)
                    return
            await send_actual(message)

        async def send_actual(message):
            await anyio.lowlevel.checkpoint()  # Recheck cancellation after a guarded wait.
            if state['closed']:
                return
            if message['type'] == 'http.response.start':
                if state['response_started']:
                    return
                state['response_started'] = True
            try:
                await send(message)  # Catch OSError only at the actual outbound boundary.
            except OSError:
                state["closed"] = True
                api.question_event(api.request_reference(Request(scope)), "delivery_failure")
                if state["handle"] is not None:
                    await api.stop_question_admission(state["handle"])

        await run_owned_application(self.application, scope, receive_once, send_if_open, state=state,
                                    stop_admission=lambda handle: api.stop_question_admission(handle))


# Flow: test host owns app -> validated body -> immediate admission -> owned observer
# and endpoint wait -> outbound send -> stop and join HTTP tasks, retaining worker leases.
