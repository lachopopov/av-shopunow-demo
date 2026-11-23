# FastAPI 0.143.2 / AnyIO 4.15.1; integrated local fault doubles, no live storage.
import asyncio
import json
import threading
from copy import deepcopy
from unittest.mock import patch
import anyio
import httpx
import fastapi.routing
from shopunow import admission, api
from shopunow.allowances import Allowance
from tests.test_identity_http import IdentityHTTPHarness, TOKEN
from tests.test_owned_operations import OwnedStorage, NOW, SECOND
from tests.test_records import USER_ID, CONVERSATION_ID, event_result
from tests.test_identity_reads import EMPTY, preference_result
from uuid import UUID


class OwnedHTTPHarness(IdentityHTTPHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.storage = OwnedStorage()
        self.readers = self.storage
        self.storage.events = lambda conversation_id, *, limit, timeout: event_result()
        self.storage.exact_offer = lambda offer_id, *, timeout: (_ for _ in ()).throw(AssertionError('No offers expected'))
        self.reservations = admission.ResourceReservations()
        self.operation = 'create'

    def app(self, **options):
        self.assertTrue(callable(getattr(api, 'owned_record_response', None)),
                        'Missing owned-record HTTP integration')
        from tests.owned_http_app import OwnedRecordsHTTPApp
        if not hasattr(self, 'tracking'):
            self.tracking = admission.OwnedRecordTracking()
        self.storage.tracking = self.tracking
        return OwnedRecordsHTTPApp(api_token=TOKEN, slots=self.slots, allowances=self.allowances,
            executor=options.pop('executor', self.executor), readers=self.storage,
            roster=frozenset({USER_ID}), projector=api.project_identity_read,
            clock=lambda:self.now, tracking=self.tracking, reservations=self.reservations,
            utc_now=lambda:NOW, uuid_factory=lambda:UUID(CONVERSATION_ID), **options)

    async def request(self, app, operation, *, content=None, params=None, headers=None):
        method, path = {'create':('POST','/conversations'), 'list':('GET','/conversations'),
                        'history':('GET','/history'), 'update':('PUT','/preferences'),
                        'read':('GET','/preferences')}[operation]
        if content is None:
            content = json.dumps({'user_id':USER_ID, **({'key':'response_style', 'value':'brief'}
                                  if operation == 'update' else {})}).encode() if operation in ('create','update') else b''
        if params is None:
            params = {'user_id':USER_ID, **({'conversation_id':CONVERSATION_ID}
                      if operation == 'history' else {})} if method == 'GET' else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url='http://test') as client:
            return await client.request(method,path,params=params,content=content,
                headers={'x-api-token':TOKEN} if headers is None else headers)

    def wire(self):
        scope, messages, sent, send = super().wire()
        method,path = {'create':('POST','/conversations'), 'list':('GET','/conversations'),
                       'history':('GET','/history'), 'update':('PUT','/preferences')}[self.operation]
        scope.update(method=method,path=path,raw_path=path.encode(), query_string=b'')
        if method == 'GET':
            scope['query_string'] = ('user_id='+USER_ID+('&conversation_id='+CONVERSATION_ID
                                      if self.operation == 'history' else '')).encode()
        else:
            messages.get_nowait()
            body = {'user_id':USER_ID}
            if self.operation == "update":
                body.update(key="response_style", value="brief")
            messages.put_nowait({'type':'http.request','body':json.dumps(body).encode(),'more_body':False})
        return scope,messages,sent,send

    def remaining(self, category=Allowance.WRITE):
        return sum(self.allowances.try_commit(category) for _ in range(31))

class OwnedHTTPTests(OwnedHTTPHarness):
    async def test_create_list_history_use_same_verified_creation(self):
        app=self.app()
        created=await self.request(app,'create')
        self.assertEqual(created.status_code,200)
        self.assertEqual(created.json()['conversation_id'],CONVERSATION_ID)
        listed=await self.request(app,'list')
        self.assertEqual([r['conversation_id'] for r in listed.json()['conversations']],
                         [CONVERSATION_ID,SECOND])
        history=await self.request(app,'history')
        self.assertEqual(history.status_code,200)
        self.assertEqual(history.json()['messages'],[])
        self.assertFalse(self.tracking.excludes((CONVERSATION_ID,)))
        self.assertEqual(self.remaining(),9)
        self.assertEqual(self.remaining(Allowance.HISTORY),28)
        self.assert_joined(app)

    async def test_unverified_creation_is_excluded_from_listing_and_history(self):
        app=self.app()
        self.storage.hooks['header']=lambda:setattr(self.storage,'saved_header',None)
        self.storage.header_data=deepcopy(EMPTY)
        self.assert_error(await self.request(app,'create'),500,'technical_error')
        listed=await self.request(app,'list')
        self.assertEqual([r['conversation_id'] for r in listed.json()['conversations']],[SECOND])
        # A visible owned header cannot override the authoritative unresolved creation.
        from tests.test_records import header_result
        self.storage.header_data=header_result()
        self.assert_error(await self.request(app,'history'),409,'conversation_unresolved')
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))
        self.assertEqual(self.remaining(Allowance.HISTORY),29)
        self.assert_joined(app)

    async def test_header_listing_does_not_certify_transcript_readability(self):
        app=self.app()
        self.storage.events=lambda *args,**kwargs:{'ids':['broken']}
        self.assertEqual((await self.request(app,'list')).status_code,200)
        self.assert_error(await self.request(app,'history'),409,'conversation_unresolved')
        self.assertEqual(self.remaining(Allowance.HISTORY),29)

    async def test_preference_update_read_and_repeated_explicit_update(self):
        app=self.app()
        for expected in [1,2]:
            response=await self.request(app,'update')
            self.assertEqual(response.status_code,200)
            self.assertEqual({k:v for k,v in response.json().items() if k!='request_id'},
                             {'user_id':USER_ID,'key':'response_style','value':'brief'})
            self.assertEqual(self.storage.saved_preference.version,expected)
            read=await self.request(app,'read')
            self.assertEqual(read.json()['response_style'],'brief')
        self.assertEqual(self.remaining(),8)
        self.assertEqual(self.remaining(Allowance.PROFILE_READ),28)
        self.assert_joined(app)

    async def test_uncertain_preference_read_remains_available_without_clearing_hold(self):
        app=self.app()
        def writer_failed():raise RuntimeError('PRIVATE uncertain write')
        self.storage.hooks['write_preference']=writer_failed
        with self.assertLogs('shopunow.entry',level='INFO') as captured:
            self.assert_error(await self.request(app,'update'),500,'technical_error')
            for value in [deepcopy(EMPTY),preference_result('detailed')]:
                self.storage.preference_data=value
                read=await self.request(app,'read')
                self.assertEqual(read.status_code,200)
                self.assertEqual(read.json()['response_style'],None if not value['ids'] else 'detailed')
                self.assertTrue(self.tracking.preference_unresolved(USER_ID))
            self.assert_error(await self.request(app,'update'),409,'preference_busy')
        self.assertNotIn('PRIVATE',' '.join(captured.output))
        self.assertEqual(self.remaining(),9)

    async def test_validation_precedes_all_owned_work(self):
        app=self.app()
        for operation in ['create','list','history','update']:
            with self.subTest(operation=operation):
                self.assertEqual((await self.request(app,operation,headers={},content=b'x'*32769)).status_code,401)
                self.assertEqual((await self.request(app,operation,content=b'x'*32769)).status_code,413)
        for content in [b'{', b'{"user_id":"syn-cust-01","key":"other","value":"brief"}',
                        b'{"user_id":"syn-cust-01","key":"response_style","value":"other"}']:
            self.assertEqual((await self.request(app,'update',content=content)).status_code,422)
        self.assertEqual((await self.request(app,'create',content=b'{"user_id":"syn-cust-01","conversation_id":"x"}')).status_code,422)
        for operation in ['list','history']:
            self.assertEqual((await self.request(app,operation,params=[('user_id',USER_ID),('user_id',USER_ID)])).status_code,422)
        self.assertEqual(self.storage.calls,[])
        self.assertEqual(self.remaining(),10)
        self.assertEqual(self.remaining(Allowance.HISTORY),30)
        self.assert_joined(app)

    async def test_exclusion_during_serialization_suppresses_entire_success_then_list_omits(self):
        for operation in ['create','list','history']:
            with self.subTest(operation=operation):
                self.tracking=admission.OwnedRecordTracking()
                app=self.app()
                original=fastapi.routing.serialize_response
                async def excluded(**kwargs):
                    value=await original(**kwargs)
                    self.tracking.exclude_conversation(CONVERSATION_ID)
                    return value
                with patch.object(fastapi.routing,'serialize_response',excluded):
                    response=await self.request(app,operation)
                self.assert_error(response,409,'conversation_unresolved')
                self.assertNotIn(CONVERSATION_ID,response.text)
                listed=await self.request(app,'list')
                self.assertEqual([r['conversation_id'] for r in listed.json()['conversations']],[SECOND])
                self.assert_joined(app)
        self.assertEqual(self.remaining(),9)
        self.assertEqual(self.remaining(Allowance.HISTORY),25)

    async def test_expiry_wins_over_exclusion_at_final_authorization(self):
        app = self.app()
        original = fastapi.routing.serialize_response

        async def late(**kwargs):
            value=await original(**kwargs)
            self.tracking.exclude_conversation(CONVERSATION_ID)
            self.now = 115.0
            return value
        with patch.object(fastapi.routing,'serialize_response',late):
            response=await self.request(app,'create')
        self.assert_error(response,503,'request_timeout')
        self.assertEqual(self.remaining(),9)
        self.assertTrue(self.storage.saved_header is not None)

    async def test_full_capacity_rejects_all_operations_without_reads(self):
        app = self.app()
        finish = threading.Event()
        self.release_events.append(finish)
        leases=[self.slots.submit(self.executor,finish.wait,3) for _ in range(5)]
        try:
            for operation in ['create','list','history','update']:
                self.assert_error(await self.request(app,operation),503,'execution_capacity')
            self.assertEqual(self.storage.calls,[])
        finally:
            finish.set()
        for lease in leases:
            await lease.wait(2)

    async def test_final_send_failure_has_no_second_response_or_delivery_claim(self):
        for operation in ['create','list','history','update']:
            with self.subTest(operation=operation):
                self.operation=operation
                if operation == "create":
                    self.tracking = admission.OwnedRecordTracking()
                app = self.app()
                scope, messages, sent, _ = self.wire()
                calls = []

                async def failed(message):
                    calls.append(message)
                    raise OSError("PRIVATE send failure")

                with self.assertLogs('shopunow.entry',level='INFO') as captured:
                    await app(scope,messages.get,failed)
                self.assertEqual([r['type'] for r in calls],['http.response.start'])
                self.assertNotIn('PRIVATE',' '.join(captured.output))
                self.assert_joined(app)

    async def test_disconnect_and_direct_cancellation_join_but_retain_worker_capacity(self):
        for operation in ['create','list','history','update']:
            for reason in ['disconnect','direct']:
                with self.subTest(operation=operation,reason=reason):
                    self.operation = operation
                    started, finish = self.block_read()
                    app = self.app()
                    task,messages,sent,state=await self.start_wire(app)
                    await self.thread_event(started)
                    if reason == "disconnect":
                        messages.put_nowait({"type": "http.disconnect"})
                    else:
                        task.cancel()
                    try:
                        await asyncio.wait_for(task, 2)
                    except asyncio.CancelledError:
                        self.assertEqual(reason, "direct")
                    self.assertEqual(sent, [])
                    self.assert_joined(app)
                    await self.assert_running_capacity_retained()
                    finish.set()
                    self.assertEqual((await state['handle'].lease.wait(2)).status,'stopped')

    async def test_anyio_scope_cancellation_is_separate_for_owned_operations(self):
        for operation in ['create','list','history','update']:
            self.operation = operation
            started, finish = self.block_read()
            app = self.app()
            scope, messages, sent, send = self.wire()
            entered = anyio.Event()
            done = anyio.Event()
            scopes = []

            async def server():
                try:
                    with anyio.CancelScope() as owner:
                        scopes.append(owner)
                        entered.set()
                        await app(scope, messages.get, send)
                finally:
                    done.set()

            async with anyio.create_task_group() as group:
                group.start_soon(server)
                await entered.wait()
                await self.thread_event(started)
                scopes[0].cancel()
                await done.wait()
            self.assertTrue(scopes[0].cancelled_caught)
            self.assertEqual(sent, [])
            self.assert_joined(app)
            finish.set()
            await app.states[-1]['handle'].lease.wait(2)

    async def test_repeated_direct_cancellation_joins_cleanup_waiting_for_thread_token(self):
        self.operation = "update"
        app = self.app()
        sending = anyio.Event()

        async def delayed(message):
            sending.set()
            await anyio.sleep_forever()

        task,_,_,state=await self.start_wire(app,send=delayed)
        await sending.wait()
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()
        try:
            task.cancel()
            await state["cleanup_started"].wait()
            task.cancel()
            task.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(task.done())
        finally:
            limiter.release()
            limiter.total_tokens = original
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assert_joined(app)
        self.assertEqual(self.remaining(), 9)

    async def test_guard_wait_keeps_loop_responsive_and_resamples_deadline_or_exclusion(self):
        from tests.test_admission import ObservedLock
        for operation in ['create','list','history','update']:
            for failure in (['exclude','expire'] if operation!='update' else ['expire']):
                with self.subTest(operation=operation,failure=failure):
                    self.now = 100.0
                    self.tracking = admission.OwnedRecordTracking()
                    app = self.app()
                    native = threading.Lock()
                    entered = threading.Event()
                    locked = []
                    original=fastapi.routing.serialize_response
                    async def delay(**kwargs):
                        result=await original(**kwargs)
                        native.acquire()
                        locked.append(True)
                        self.tracking._lock=ObservedLock(native,entered.set)
                        return result
                    with patch.object(fastapi.routing,'serialize_response',delay):
                        task = asyncio.create_task(self.request(app, operation))
                        self.hosts.append(task)
                        try:
                            await self.thread_event(entered)
                            await anyio.lowlevel.checkpoint()
                            self.assertFalse(task.done())  # Event loop runs while control lock waits.
                            if failure == "expire":
                                self.now = 115.0
                            else:
                                self.tracking._other_exclusions.add(CONVERSATION_ID)
                        finally:
                            if locked:
                                native.release()
                        response=await asyncio.wait_for(task,2)
                    self.assert_error(response,503 if failure=='expire' else 409,
                                      'request_timeout' if failure=='expire' else 'conversation_unresolved')
                    self.assert_joined(app)

    async def test_exclusion_after_authorization_does_not_replace_started_success(self):
        for operation in ['create','list','history']:
            with self.subTest(operation=operation):
                self.operation = operation
                self.tracking = admission.OwnedRecordTracking()
                app = self.app()
                scope,messages,sent,send=self.wire()
                async def exclude_after_start(message):
                    await send(message)
                    if message['type']=='http.response.start':
                        self.tracking.exclude_conversation(CONVERSATION_ID)
                await app(scope,messages.get,exclude_after_start)
                self.assertEqual([r['status'] for r in sent if r['type']=='http.response.start'],[200])
                self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))
                fresh=await self.request(app,'list')
                self.assertEqual([r['conversation_id'] for r in fresh.json()['conversations']],[SECOND])
                self.assert_joined(app)

    async def test_disconnected_preference_writer_retains_capacity_and_uncertainty(self):
        self.operation = "update"
        app = self.app()
        started = threading.Event()
        finish = threading.Event()
        self.release_events.append(finish)
        def blocked():
            started.set()
            if not finish.wait(3):
                raise RuntimeError("barrier timeout")

        self.storage.hooks['write_preference']=blocked
        task,messages,sent,state=await self.start_wire(app)
        await self.thread_event(started)
        messages.put_nowait({'type':'http.disconnect'})
        await asyncio.wait_for(task,2)
        self.assertEqual(sent, [])
        self.assert_joined(app)
        await self.assert_running_capacity_retained()
        self.assertTrue(self.tracking.preference_unresolved(USER_ID))
        finish.set()
        self.assertEqual((await state['handle'].lease.wait(2)).status,'stopped')
        read=await self.request(app,'read')
        self.assertEqual(read.json()['response_style'],'brief')
        self.assertTrue(self.tracking.preference_unresolved(USER_ID))
        self.assert_error(await self.request(app,'update'),409,'preference_busy')
        self.assertEqual(self.remaining(),9)

    async def test_response_identity_changes_and_serialization_failures_remain_safe(self):
        original=fastapi.routing.serialize_response
        for operation in ['create','list','history','update']:
            for failure in ['identity','raise']:
                with self.subTest(operation=operation,failure=failure):
                    self.tracking = admission.OwnedRecordTracking()
                    app = self.app()

                    async def changed(**kwargs):
                        if failure == "raise":
                            raise ValueError("PRIVATE serialization failure")
                        object.__setattr__(kwargs['response_content'],'user_id','syn-cust-02')
                        return await original(**kwargs)
                    with self.assertLogs('shopunow.entry',level='INFO') as captured:
                        with patch.object(fastapi.routing,'serialize_response',changed):
                            response=await self.request(app,operation)
                    self.assert_error(response,500,'technical_error')
                    self.assertNotIn('PRIVATE',response.text+' '.join(captured.output))
                    self.assert_joined(app)

    async def test_owned_application_events_exclude_request_and_stored_content(self):
        app=self.app()
        with self.assertLogs('shopunow.entry',level='INFO') as captured:
            for operation in ['create','list','history','update']:
                await self.request(app,operation)
            await self.request(app,'update',content=b'{"PRIVATE invalid input"}')
            self.storage.hooks['headers']=lambda:(_ for _ in ()).throw(RuntimeError('PRIVATE SDK fault'))
            await self.request(app,'list')
        events=[json.loads(row.split(':',2)[2]) for row in captured.output]
        for event in events:
            self.assertLessEqual(set(event),{'utc_time','event','request_id','outcome','http_status','elapsed_ms'})
        for forbidden in [TOKEN,USER_ID,CONVERSATION_ID,'PRIVATE','response_style','Traceback']:
            self.assertNotIn(forbidden,' '.join(captured.output))
        self.assert_joined(app)

    async def test_running_writes_are_not_failures_before_their_readback(self):
        from tests.test_admission import ObservedThreadPool
        for operation in ['create','update']:
            with self.subTest(operation=operation):
                self.tracking=admission.OwnedRecordTracking()
                started = threading.Event()
                finish = threading.Event()
                self.release_events.append(finish)

                def blocked():
                    started.set()
                    if not finish.wait(3):
                        raise RuntimeError("writer barrier expired")

                self.storage.hooks['write_header' if operation=='create' else 'write_preference']=blocked
                class WriterStartedExecutor(ObservedThreadPool):
                    def submit(self,fn,*args,**kwargs):
                        future=super().submit(fn,*args,**kwargs)
                        if not started.wait(2):
                            raise RuntimeError("writer did not start")
                        return future
                executor=WriterStartedExecutor(max_workers=1)
                self.addCleanup(executor.shutdown,wait=True,cancel_futures=True)
                app = self.app(executor=executor)
                checked = anyio.Event()
                observed = []
                original=api.history_admission_state
                async def inspect(*args,**kwargs):
                    value = await original(*args, **kwargs)
                    observed.append(value.code)
                    checked.set()
                    return value

                with patch.object(api,'history_admission_state',inspect):
                    task = asyncio.create_task(self.request(app, operation))
                    self.hosts.append(task)
                    try:
                        await asyncio.wait_for(checked.wait(),2)
                        self.assertIsNone(observed[0])
                        self.assertFalse(task.done())
                    finally:
                        finish.set()
                    response=await asyncio.wait_for(task,2)
                self.assertEqual(response.status_code,200)
                self.assert_joined(app)

    async def test_exclusion_does_not_override_profile_or_absent_foreign_ownership_failure(self):
        from tests.test_records import header_result,HEADER

        app = self.app()
        self.tracking.exclude_conversation(CONVERSATION_ID)
        foreign=header_result(dict(HEADER,owner_user_id='syn-cust-02'))
        foreign['metadatas'][0]['owner_user_id']='syn-cust-02'  # Consistent valid foreign header.
        for native in [deepcopy(EMPTY), foreign]:
            self.storage.header_data=native
            self.assert_error(await self.request(app,'history'),404,'ownership_failure')
        self.storage.profile_data=deepcopy(EMPTY)
        self.assert_error(await self.request(app,'history'),404,'profile_unavailable')
        self.assertEqual(self.remaining(Allowance.HISTORY),30)

    async def test_exclusion_after_failed_allowance_hit_does_not_override_429(self):
        for _ in range(30):
            self.allowances.try_commit(Allowance.HISTORY)
        app = self.app()
        original = self.allowances._try_commit_locked

        def excluded(category):
            answer=original(category)
            self.tracking.exclude_conversation(CONVERSATION_ID)
            return answer
        with patch.object(self.allowances,'_try_commit_locked',excluded):
            response=await self.request(app,'history')
        self.assert_error(response,429,'history_limit')
        self.assert_joined(app)
