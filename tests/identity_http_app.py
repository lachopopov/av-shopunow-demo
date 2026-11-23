# FastAPI v0.143.2 / AnyIO v4.15.1; injected, simulated HTTP integration only.
from time import monotonic  # Measure only observed local HTTP elapsed time.
import anyio  # Own the application and receive observer through their entire lifetimes.
from typing import Annotated  # Keep validation in FastAPI's native dependency lifecycle.
from fastapi import Depends, Request  # Use public request and dependency interfaces.

from shopunow import admission, api  # Compose the actual production boundary components.


class IdentityReadHTTPApp:
    """Test-only identity host; reuse the demonstrated history HTTP ownership pattern."""

    def __init__(self, *, api_token, application=None, **admission_options):
        self.application = application if application is not None else api.create_app()
        self.states = []  # Test observations belong here, never in production state.
        self.request_started = anyio.Event()
        profile_validate = api.profile_resolve_validator(api_token=api_token)
        preference_validate = api.preference_read_validator(api_token=api_token)

        @self.application.exception_handler(Exception)
        async def unexpected_history_error(request, error):
            # Native serialization can fail after charging; current expiry still takes priority.
            handle = request.scope["identity_http"]["handle"]
            if handle is None:
                return api.safe_error(request, 500)
            current = await api.history_admission_state(handle)
            return api.identity_error(request, current.code or "technical_error")

        @self.application.post('/profiles/resolve', response_model=api.ProfileResolveResponse)
        async def resolve_profile(request: Request, payload: Annotated[object, Depends(profile_validate)]):
            return await read_identity(request, payload, 'profile')

        @self.application.get('/preferences', response_model=api.PreferenceReadResponse)
        async def read_preference(request: Request, payload: Annotated[object, Depends(preference_validate)]):
            return await read_identity(request, payload, 'preference')

        async def read_identity(request, payload, operation):
            # The dependency finishes the single body reader before this endpoint starts.
            state = request.scope["identity_http"]
            try:
                handle = await admission.submit_identity_read_async(
                    api.IdentityReadSelectors(request_id=api.request_reference(request),
                                              user_id=payload.user_id), operation=operation,
                    on_worker_finished=api.identity_worker_observer(request),
                    **admission_options,
                )
            except admission.AdmissionBusy:
                return api.identity_error(request, "execution_capacity")
            state["handle"] = handle  # Successful submission has no suspension before assignment.
            state["submitted"].set()
            api.observe_identity_read(request, handle)
            state["group"].start_soon(observe_disconnect, request, state)
            return await api.identity_read_response(request, handle)

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

        self.observe_disconnect = observe_disconnect  # Reuse this host's owned observer for local routes.

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.application(scope, receive, send)
        state = {"started_at": monotonic(), "handle": None, "closed": False, "observer_started": False,
                 "observer_done": False, "application_done": False,
                 "submitted": anyio.Event(), "observer_ready": anyio.Event(),
                 "cleanup_started": anyio.Event(), "response_started": False,
                 "success_suppressed": False}
        self.states.append(state)
        self.request_started.set()
        scope["identity_http"] = state
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
                    replacement = api.identity_error(Request(scope), current.code)
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
                if message['type'] == 'http.response.start' and message['status'] == 200:
                    # Logging may delay: emit after start, never between authorization and start.
                    # Response readiness is not proof of body receipt or physical delivery.
                    api.identity_event(api.request_reference(Request(scope)), 'ready', 200,
                        elapsed_ms=max(0, round((monotonic()-state['started_at'])*1000)))
            except OSError:
                state["closed"] = True
                api.identity_event(api.request_reference(Request(scope)), "delivery_failure")
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
