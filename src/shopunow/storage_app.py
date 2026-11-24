# FastAPI 0.143.2 / AnyIO 4.15.1; approved storage foundation, no model or credential loader.
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Annotated
from uuid import uuid4
import anyio
from fastapi import Depends, Request
from limits.storage import MemoryStorage
from shopunow import admission, api
from shopunow.allowances import RollingAllowances
from shopunow.profiles import load_profiles
from shopunow.sdk_worker import LocalSDKWorker
from shopunow.sdk_writes import SDKRecords, SDKEvidenceRecords
from shopunow.question_host import QuestionHTTPApp, run_owned_application
from shopunow.schemas import HandoffRequest, HandoffResponse


class StorageApplication:
    """Five storage routes with existing native worker and HTTP completion ownership."""

    def __init__(self, *, connect, api_token, collection_names, clock=monotonic,
                 knowledge_collection_name=None, answering=None, progress=None):
        self.worker = LocalSDKWorker(connect)
        self.progress_host = None  # GET /progress only when a progress store is configured (owner 2026-11-23).
        if progress is not None:
            self.progress_host = api.create_app()  # Same safe error handling as every other route.
            progress_validate = api.history_validator(api_token=api_token)  # Token + user/conversation query.

            @self.progress_host.get('/progress')
            async def progress_step(payload: Annotated[object, Depends(progress_validate)]):
                # In-memory read only: no storage, no allowance; a step number, never content.
                return {'user_id': payload.user_id, 'conversation_id': payload.conversation_id,
                        'step': progress.get(payload.user_id, payload.conversation_id)}
        self.answering, self.question_host = answering, None  # /interactions only when configured.
        self.storage = None
        self.tracking = admission.OwnedRecordTracking()
        self.reservations, self.slots = admission.ResourceReservations(), admission.ExecutionSlots()
        self.allowances = RollingAllowances(MemoryStorage(), Lock())
        roster = frozenset(load_profiles())

        @asynccontextmanager
        async def lifespan(application):
            async with self.worker.lifespan(application):
                with anyio.fail_after(8):
                    collections = [await self.worker.client.get_collection(name, embedding_function=None)
                                   for name in collection_names]
                self.storage = SDKRecords(self.worker, profiles=collections[0],
                    conversations=collections[1], escalations=collections[2])
                if knowledge_collection_name is not None:
                    with anyio.fail_after(8):
                        knowledge=await self.worker.client.get_collection(
                            knowledge_collection_name,embedding_function=None)
                    self.storage=SDKEvidenceRecords(self.worker,profiles=collections[0],
                        conversations=collections[1],escalations=collections[2],knowledge=knowledge)
                    self.storage._check_knowledge_configuration()
                if answering is not None:
                    # Slice 6: the same storage, tracking and capacity as every other route.
                    self.question_host = QuestionHTTPApp(api_token=api_token, slots=self.slots,
                        reservations=self.reservations, allowances=self.allowances, executor=self.worker,
                        readers=self.storage, tracking=self.tracking, clock=clock,
                        on_admitted=answering(storage=self.storage, tracking=self.tracking))
                yield

        self.application = api.create_app(lifespan=lifespan)
        profile_validate = api.profile_resolve_validator(api_token=api_token)
        preference_validate = api.preference_read_validator(api_token=api_token)
        history_validate = api.history_validator(api_token=api_token)
        create_validate = api._json_request_validator(api_token=api_token,body_limit=32768,
                                                     request_model=api.ConversationCreateRequest)
        handoff_validate = api._json_request_validator(api_token=api_token,body_limit=32768,
                                                      request_model=HandoffRequest)

        @self.application.exception_handler(Exception)
        async def unexpected_error(request, error):
            handle = request.scope['identity_http']['handle']
            if handle is None:
                return api.safe_error(request,500)
            current = await api.history_admission_state(handle)
            return api.identity_error(request,current.code or 'technical_error')

        async def finish(request, handle, operation):
            state=request.scope['identity_http']
            state['handle']=handle
            state['submitted'].set()
            api.observe_identity_read(request,handle)
            state['group'].start_soon(self.observe_disconnect,request,state)
            if operation=='history':
                return await api.history_admission_response(request,handle)
            if operation in ('profile','preference'):
                return await api.identity_read_response(request,handle)
            return await api.owned_record_response(request,handle,operation=operation)

        async def dispatch(request,payload,operation):
            if self.storage is None:
                return api.identity_error(request,'execution_capacity')
            shared=dict(slots=self.slots,allowances=self.allowances,executor=self.worker,
                clock=clock,on_worker_finished=api.identity_worker_observer(request))
            selectors=dict(request_id=api.request_reference(request),user_id=payload.user_id)
            if payload.user_id not in roster:
                # Reuse the existing roster rejection under the shared slot, before any storage/hit.
                # Do not require a prior resolve call or replace authoritative stored bindings.
                operation='profile'
            try:
                if operation in ('profile','preference'):
                    handle=await admission.submit_identity_read_async(selectors,operation=operation,
                        readers=self.storage,roster=roster,projector=api.project_identity_read,**shared)
                elif operation=='create':
                    handle=await admission.submit_conversation_creation_async(user_id=payload.user_id,
                        request_id=selectors['request_id'],reservations=self.reservations,
                        storage=self.storage,supervisor=self.tracking,tracking=self.tracking,
                        uuid_factory=uuid4,utc_now=lambda:datetime.now(timezone.utc),**shared)
                elif operation=='handoff':
                    handle=await admission.submit_handoff_async({**selectors,
                        'conversation_id':payload.conversation_id,'offer_id':payload.offer_id,
                        'name':payload.name,'phone':payload.phone,'email':payload.email},
                        reservations=self.reservations,storage=self.storage,tracking=self.tracking,**shared)
                elif operation=='list':
                    handle=await admission.submit_conversation_list_async(selectors,readers=self.storage,
                        tracking=self.tracking,**shared)
                else:
                    handle=await admission.submit_history_admission_async({**selectors,
                        'conversation_id':payload.conversation_id},readers=self.storage,
                        tracking=self.tracking,projector=api.project_history_response,**shared)
            except admission.AdmissionBusy:
                return api.identity_error(request,'execution_capacity')
            return await finish(request,handle,operation)

        @self.application.post('/profiles/resolve',response_model=api.ProfileResolveResponse)
        async def profile(request:Request,payload:Annotated[object,Depends(profile_validate)]):
            return await dispatch(request,payload,'profile')

        @self.application.get('/preferences',response_model=api.PreferenceReadResponse)
        async def preference(request:Request,payload:Annotated[object,Depends(preference_validate)]):
            return await dispatch(request,payload,'preference')

        @self.application.post('/conversations',response_model=api.ConversationCreateResponse)
        async def create(request:Request,payload:Annotated[object,Depends(create_validate)]):
            return await dispatch(request,payload,'create')

        @self.application.post('/handoffs',response_model=HandoffResponse)
        async def handoff(request:Request,payload:Annotated[object,Depends(handoff_validate)]):
            return await dispatch(request,payload,'handoff')

        @self.application.get('/conversations',response_model=api.ConversationListResponse)
        async def listing(request:Request,payload:Annotated[object,Depends(preference_validate)]):
            return await dispatch(request,payload,'list')

        @self.application.get('/history',response_model=api.HistoryResponse)
        async def history(request:Request,payload:Annotated[object,Depends(history_validate)]):
            return await dispatch(request,payload,'history')

    async def observe_disconnect(self, request, state):
        state['observer_started']=True
        state['observer_ready'].set()
        try:
            while True:
                if (await request.receive())['type']=='http.disconnect':
                    state['closed']=True
                    await api.stop_history_admission(state['handle'])
                    state['application_scope'].cancel()
                    return
        finally:
            state['observer_done']=True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.application(scope, receive, send)
        if scope["path"] == "/progress" and self.progress_host is not None:
            return await self.progress_host(scope, receive, send)  # Plain read; no owned lifecycle needed.
        if scope["path"] == "/interactions" and self.answering is not None:
            if self.question_host is None:  # Not started yet: same safe capacity error as other routes.
                response = api.question_error(Request(scope), "execution_capacity")
                return await response(scope, receive, send)
            return await self.question_host(scope, receive, send)  # Its own owned request lifecycle.
        state = {"started_at": monotonic(), "handle": None, "closed": False, "observer_started": False,
                 "observer_done": False, "application_done": False,
                 "submitted": anyio.Event(), "observer_ready": anyio.Event(),
                 "cleanup_started": anyio.Event(), "response_started": False,
                 "success_suppressed": False}
        scope["identity_http"] = state

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

        await run_owned_application(self.application, scope, receive_once, send_if_open, state=state,
                                    stop_admission=lambda handle: api.stop_history_admission(handle))


def create_storage_app(*, connect, api_token, collection_names, knowledge_collection_name=None,
                       answering=None, progress=None):
    if len(collection_names)!=3 or len(set(collection_names))!=3:
        raise ValueError('Three distinct operational collection names are required.')
    if not api_token:
        raise ValueError('A nonempty API token is required.')
    if knowledge_collection_name is not None and (
            type(knowledge_collection_name) is not str or not knowledge_collection_name.strip()
            or knowledge_collection_name in collection_names):
        raise ValueError('A distinct explicitly prepared Knowledge collection is required.')
    return StorageApplication(connect=connect,api_token=api_token,collection_names=collection_names,
                              knowledge_collection_name=knowledge_collection_name,answering=answering,
                              progress=progress)


# Flow: explicit initializer -> one lifespan client/loop -> existing five-slot operations
# -> native verification and response -> joined HTTP observers, independently owned worker cleanup.
