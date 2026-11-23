# FastAPI v0.143.2 / AnyIO v4.15.1; injected, simulated HTTP integration only.
import anyio  # Own the application and receive observer through their entire lifetimes.
from typing import Annotated  # Keep validation in FastAPI's native dependency lifecycle.
from fastapi import Depends, Request  # Use public request and dependency interfaces.

from shopunow import admission, api  # Compose the actual production boundary components.


class HistoryHTTPApp:
    """Test-only ASGI host; no normally served route or global middleware is installed."""

    def __init__(self, *, api_token, **admission_options):
        self.application = api.create_app()
        self.states = []  # Test observations belong here, never in production state.
        self.request_started = anyio.Event()
        validate = api.history_validator(api_token=api_token)

        @self.application.exception_handler(Exception)
        async def unexpected_history_error(request, error):
            # Native serialization can fail after charging; current expiry still takes priority.
            handle = request.scope["history_http"]["handle"]
            if handle is None:
                return api.safe_error(request, 500)
            current = await api.history_admission_state(handle)
            return api.history_error(request, current.code or "technical_error")

        @self.application.get("/history", response_model=api.HistoryResponse)
        async def history(request: Request, payload: Annotated[object, Depends(validate)]):
            # The dependency finishes the single body reader before this endpoint starts.
            state = request.scope["history_http"]
            try:
                handle = await admission.submit_history_admission_async(
                    api.HistorySelectors(request_id=api.request_reference(request),
                                         user_id=payload.user_id,
                                         conversation_id=payload.conversation_id),
                    on_worker_finished=api.history_worker_observer(request),
                    **admission_options,
                )
            except admission.AdmissionBusy:
                return api.history_error(request, "execution_capacity")
            state["handle"] = handle  # Successful submission has no suspension before assignment.
            state["submitted"].set()
            api.observe_history_admission(request, handle)
            state["group"].start_soon(observe_disconnect, request, state)
            return await api.history_admission_response(request, handle)

        async def observe_disconnect(request, state):
            state["observer_started"] = True
            state["observer_ready"].set()
            try:
                while True:
                    message = await request.receive()  # Only listener after validated body.
                    if message["type"] == "http.disconnect":
                        state["closed"] = True
                        await api.stop_history_admission(state["handle"])
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
        self.states.append(state)
        self.request_started.set()
        scope["history_http"] = state
        finished = anyio.Event()
        errors = []  # Preserve native application exceptions after the owned group joins.

        async def receive_once():
            message = await receive()
            if message["type"] == "http.disconnect":
                state["closed"] = True  # Also cover disconnect during incomplete body input.
            return message

        async def send_if_open(message):
            await anyio.lowlevel.checkpoint()  # Deliver owner cancellation at the outbound boundary.
            if state["closed"] or state["success_suppressed"]:
                return  # Original success body and headers never follow a replacement response.
            if (message["type"] == "http.response.start" and message["status"] == 200
                    and state["handle"] is not None):
                # FastAPI has completed its native response-model validation and JSON serialization.
                current = await api.history_admission_state(state["handle"])
                if current.code is not None:
                    state["success_suppressed"] = True
                    replacement = api.history_error(Request(scope), current.code)
                    await replacement(scope, receive_once, send_actual)
                    return  # Emit the complete safe response with its own native headers.
            await send_actual(message)

        async def send_actual(message):
            await anyio.lowlevel.checkpoint()  # Recheck after a delayed guarded control read.
            if state["closed"]:
                return
            if message["type"] == "http.response.start":
                if state["response_started"]:
                    return  # A response already started; never attempt another error response.
                state["response_started"] = True
            try:
                await send(message)
            except OSError:
                state["closed"] = True
                api.history_event(api.request_reference(Request(scope)), "delivery_failure")
                if state["handle"] is not None:
                    await api.stop_history_admission(state["handle"])

        async def run_application():
            try:
                with anyio.CancelScope() as application_scope:
                    state["application_scope"] = application_scope
                    try:
                        await self.application(scope, receive_once, send_if_open)
                    finally:
                        # Shield before the first await, including thread-token acquisition.
                        with anyio.CancelScope(shield=True):
                            state["cleanup_started"].set()
                            if state["handle"] is not None:
                                await api.stop_history_admission(state["handle"])
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


# Flow: test host owns app -> validated body -> immediate admission -> owned observer
# and endpoint wait -> outbound send -> stop and join HTTP tasks, retaining worker leases.
