# FastAPI 0.143.2 / AnyIO 4.15.1; only injected local routes, no providers or seeding.
from typing import Annotated
from fastapi import Depends, Request
from shopunow import admission, api
from tests.identity_http_app import IdentityReadHTTPApp


class OwnedRecordsHTTPApp(IdentityReadHTTPApp):
    """Reuse the demonstrated observer/send ownership, with one shared local tracking owner."""

    def __init__(self, *, tracking, reservations, utc_now, uuid_factory, **options):
        super().__init__(**options)
        self.tracking = tracking
        readers = options['readers']
        shared = {key:options[key] for key in ('slots','allowances','executor','clock')}
        create_validate = api._json_request_validator(api_token=options['api_token'],body_limit=32768,
                                                       request_model=api.ConversationCreateRequest)
        list_validate = api.preference_read_validator(api_token=options['api_token'])
        history_validate = api.history_validator(api_token=options['api_token'])
        update_validate = api._json_request_validator(api_token=options['api_token'],body_limit=32768,
                                                       request_model=api.PreferenceUpdateRequest)

        async def finish(request, handle, operation):
            state=request.scope['identity_http']
            state['handle']=handle  # No suspension between dispatch and ownership recording.
            state['submitted'].set()
            api.observe_identity_read(request,handle)
            state['group'].start_soon(self.observe_disconnect,request,state)
            if operation == "history":
                return await api.history_admission_response(request, handle)
            return await api.owned_record_response(request,handle,operation=operation)

        @self.application.post('/conversations',response_model=api.ConversationCreateResponse)
        async def create(request:Request,payload:Annotated[object,Depends(create_validate)]):
            try:
                handle=await admission.submit_conversation_creation_async(user_id=payload.user_id,
                    request_id=api.request_reference(request), reservations=reservations,
                    storage=readers,supervisor=tracking,tracking=tracking,utc_now=utc_now,
                    uuid_factory=uuid_factory,on_worker_finished=api.identity_worker_observer(request),**shared)
            except admission.AdmissionBusy:
                return api.identity_error(request, "execution_capacity")
            return await finish(request,handle,'create')

        @self.application.get('/conversations',response_model=api.ConversationListResponse)
        async def listing(request:Request,payload:Annotated[object,Depends(list_validate)]):
            try:
                handle=await admission.submit_conversation_list_async(
                    api.IdentityReadSelectors(request_id=api.request_reference(request),user_id=payload.user_id),
                    readers=readers,tracking=tracking,
                    on_worker_finished=api.identity_worker_observer(request),**shared)
            except admission.AdmissionBusy:
                return api.identity_error(request, "execution_capacity")
            return await finish(request,handle,'list')

        @self.application.get('/history',response_model=api.HistoryResponse)
        async def history(request:Request,payload:Annotated[object,Depends(history_validate)]):
            try:
                handle=await admission.submit_history_admission_async(
                    api.HistorySelectors(request_id=api.request_reference(request),user_id=payload.user_id,
                                         conversation_id=payload.conversation_id),
                    readers=readers,projector=api.project_history_response,tracking=tracking,
                    on_worker_finished=api.identity_worker_observer(request),**shared)
            except admission.AdmissionBusy:
                return api.identity_error(request, "execution_capacity")
            return await finish(request,handle,'history')

        @self.application.put('/preferences',response_model=api.PreferenceUpdateResponse)
        async def update(request:Request,payload:Annotated[object,Depends(update_validate)]):
            try:
                handle=await admission.submit_preference_update_async(
                    api.PreferenceUpdateSelectors(request_id=api.request_reference(request),
                        user_id=payload.user_id,key=payload.key,value=payload.value),
                    storage=readers,tracking=tracking,reservations=reservations,utc_now=utc_now,
                    on_worker_finished=api.identity_worker_observer(request),**shared)
            except admission.AdmissionBusy:
                return api.identity_error(request, "execution_capacity")
            return await finish(request,handle,'update')
