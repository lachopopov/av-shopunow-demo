# FastAPI 0.143.2; cross-operation checks use one actual local pool/limiter/reservation owner.
import asyncio
import json
import threading
import httpx
from shopunow import admission
from shopunow.allowances import Allowance
from tests.test_owned_http import OwnedHTTPHarness
from tests.question_http_app import QuestionHTTPApp
from tests.test_identity_http import TOKEN
from tests.test_records import USER_ID,CONVERSATION_ID


class Slice1AcceptanceTests(OwnedHTTPHarness):
    def question_app(self, *, use_tracking=True):
        if not hasattr(self, "tracking"):
            self.app()
        from tests.verified_creation import verified_tracking
        options={'tracking':self.tracking if use_tracking else verified_tracking()}
        try:
            return QuestionHTTPApp(api_token=TOKEN,slots=self.slots,reservations=self.reservations,
                allowances=self.allowances,executor=self.executor,readers=self.storage,
                clock=lambda:self.now,**options)
        except TypeError as error:
            self.fail('Missing shared held-conversation integration: '+type(error).__name__)

    async def question(self,app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
            return await client.post('/interactions',headers={'x-api-token':TOKEN},
                json={'user_id':USER_ID,'conversation_id':CONVERSATION_ID,'question':'Synthetic question?'})

    async def test_unknown_creation_cannot_bypass_hold_through_question_admission(self):
        app=self.app()
        def uncertain():raise RuntimeError('synthetic unknown write')
        self.storage.hooks['write_header']=uncertain
        self.assert_error(await self.request(app,'create'),500,'technical_error')
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))
        # The write may have saved a valid header; this does not resolve retained uncertainty.
        self.assertIsNotNone(self.storage.saved_header)
        self.assert_error(await self.question(self.question_app()),409,'conversation_unresolved')
        self.assertEqual(self.remaining(Allowance.QUESTION),10)
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))

    async def test_five_mixed_workers_share_capacity_without_cross_category_charges(self):
        app = self.app()
        question_app = self.question_app(use_tracking=False)
        finish = threading.Event()
        entered = threading.Semaphore(0)
        self.release_events.append(finish)

        def blocked():
            entered.release()
            if not finish.wait(4):
                raise RuntimeError("controlled profile barrier")

        self.storage.hooks['profile']=blocked
        tasks=[]
        for operation in ['question','history','list','read','profile']:
            if operation == "question":
                call = self.question(question_app)
            elif operation == "profile":
                call = self.get(app, operation="profile")
            else:
                call = self.request(app, operation)
            tasks.append(asyncio.create_task(call))
            self.hosts.append(tasks[-1])
            self.assertTrue(await __import__('anyio').to_thread.run_sync(entered.acquire,True,2))
        before=len(self.storage.calls)
        self.assert_error(await self.request(app,'update'),503,'execution_capacity')
        self.assertEqual(len(self.storage.calls),before)
        finish.set()
        results=await asyncio.wait_for(asyncio.gather(*tasks),3)
        self.assert_error(results[0],503,'interaction_not_ready')
        self.assertEqual([r.status_code for r in results[1:]],[200,200,200,200])
        self.assertEqual(self.remaining(Allowance.QUESTION),9)
        self.assertEqual(self.remaining(Allowance.HISTORY),28)
        self.assertEqual(self.remaining(Allowance.PROFILE_READ),28)
        self.assertEqual(self.remaining(Allowance.WRITE),10)
        self.assert_joined(app)

    async def test_create_and_update_share_write_allowance_but_reads_do_not(self):
        app=self.app()
        for _ in range(9):
            self.assertTrue(self.allowances.try_commit(Allowance.WRITE))
        self.assertEqual((await self.request(app,'create')).status_code,200)
        self.assert_error(await self.request(app,'update'),429,'write_limit')
        self.assertEqual((await self.request(app,'list')).status_code,200)
        self.assertEqual((await self.request(app,'read')).status_code,200)
        self.assertNotIn('write_preference',[row[0] for row in self.storage.calls])

    async def test_health_needs_no_slot_or_allowance_even_when_all_are_exhausted(self):
        app=self.app()
        for category in Allowance:
            while self.allowances.try_commit(category):
                pass
        finish = threading.Event()
        self.release_events.append(finish)
        leases=[self.slots.submit(self.executor,finish.wait,3) for _ in range(5)]
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.get('/health')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json(),{'status':'ok'})
            self.assertEqual(self.storage.calls,[])
        finally:
            finish.set()
        for lease in leases:
            await lease.wait(2)

    async def test_admission_and_real_capacity_release_are_logged_without_content(self):
        app=self.app()
        with self.assertLogs('shopunow.entry',level='INFO') as captured:
            await self.request(app,'read')
        events=[json.loads(row.split(':',2)[2]) for row in captured.output]
        self.assertTrue(any(row['event']=='identity_admission' and row['outcome']=='admitted' for row in events))
        self.assertTrue(any(row['event']=='identity_capacity_released' for row in events))
        for text in [USER_ID, CONVERSATION_ID, TOKEN]:
            self.assertNotIn(text, " ".join(captured.output))

    async def test_hold_recorded_during_transcript_read_stops_before_allowance(self):
        from tests.test_records import event_result
        _app=self.app()
        from tests.verified_creation import verified_tracking
        verified_tracking(self.tracking)
        def events(*args,**kwargs):
            self.tracking.exclude_conversation(CONVERSATION_ID)
            return event_result()
        self.storage.events=events
        self.assert_error(await self.question(self.question_app()),409,'conversation_unresolved')
        self.assertEqual(self.remaining(Allowance.QUESTION),10)

    async def test_tracking_wait_expiry_wins_over_pre_read_hold(self):
        from tests.test_admission import ObservedLock

        _app = self.app()
        native = threading.Lock()
        entered = threading.Event()
        native.acquire()
        self.tracking._lock = ObservedLock(native, entered.set)
        handle=admission.submit_question_admission(
            {'user_id':USER_ID,'conversation_id':CONVERSATION_ID,'question':'Synthetic question?'},
            slots=self.slots,reservations=self.reservations,allowances=self.allowances,
            executor=self.executor,readers=self.storage,clock=lambda:self.now,tracking=self.tracking)
        try:
            await self.thread_event(entered)
            self.tracking._other_exclusions.add(CONVERSATION_ID)
            self.now = 115.0
        finally:
            native.release()
        self.assertEqual((await handle.lease.wait(2)).status,'stopped')
        self.assertEqual(self.remaining(Allowance.QUESTION),10)

    async def test_hold_recorded_after_hit_forbids_normal_work_without_undoing_charge(self):
        from tests.test_records import event_result

        _app = self.app()
        from tests.verified_creation import verified_tracking
        verified_tracking(self.tracking)
        entered = threading.Event()
        finish = threading.Event()
        self.release_events.append(finish)

        def events(*args,**kwargs):
            entered.set()
            if not finish.wait(3):
                raise RuntimeError("controlled event wait")
            return event_result()
        self.storage.events=events
        handle=admission.submit_question_admission(
            {'user_id':USER_ID,'conversation_id':CONVERSATION_ID,'question':'Synthetic question?'},
            slots=self.slots,reservations=self.reservations,allowances=self.allowances,
            executor=self.executor,readers=self.storage,clock=lambda:self.now,tracking=self.tracking)
        await self.thread_event(entered)
        original=handle.attempt.authorize_normal_work
        def newly_held():
            self.tracking.exclude_conversation(CONVERSATION_ID)
            return original()
        handle.attempt.authorize_normal_work=newly_held
        finish.set()
        result=await handle.lease.wait(2)
        self.assertEqual(result.status,'conversation_unresolved')
        self.assertTrue(result.charged)
        self.assertEqual(self.remaining(Allowance.QUESTION),9)
