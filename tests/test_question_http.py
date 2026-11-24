# Python v3.12 / HTTPX v0.28.1 / AnyIO v4.15.1; every storage/fault case is simulated.
import asyncio  # Exercise direct server-facing cancellation separately from AnyIO scopes.
import json  # Encode literal wire data and inspect sanitized operational events.
import threading  # Hold real worker boundaries without scheduling sleeps.
import unittest  # Reuse the project's native offline test runner.
from time import monotonic  # Let only the feature's real timeout advance in deadline tests.
from unittest.mock import patch  # Control only native hit/dispatch fault boundaries.
from uuid import UUID  # Check opaque references and independently build historical selectors.

import anyio  # Verify native task groups, shields and worker-thread limiter ownership.
import httpx  # Real FastAPI requests for ordinary completed-response envelopes.
from limits.storage import MemoryStorage  # Keep quota accounting real.
from starlette.requests import ClientDisconnect  # Preserve native incomplete-body disconnect behavior.

from shopunow import admission, api
from shopunow.allowances import Allowance, RollingAllowances
from tests.question_http_app import QuestionHTTPApp
from tests.test_admission import InlineExecutor, ObservedThreadPool, SubmitThenRaise
from tests.test_question_admission import SimulatedReaders
from tests.test_records import (CONVERSATION_ID, HEADER, USER_ID, event_result, header_result,
                               transcript_marker, transcript_offer, transcript_pair)


TOKEN = "local-test-token"
VALID = {"user_id": USER_ID, "conversation_id": CONVERSATION_ID, "question": "Private question?"}


class QuestionHTTPTests(unittest.IsolatedAsyncioTestCase):
    """Catch wrong public mapping, lost controls and premature worker-resource release."""

    async def asyncSetUp(self):
        from tests.verified_creation import verified_tracking
        self.tracking = verified_tracking()
        self.slots = admission.ExecutionSlots()
        self.reservations = admission.ResourceReservations()
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.allowances = RollingAllowances(self.storage, threading.Lock())
        self.readers = SimulatedReaders()
        self.executor = ObservedThreadPool(max_workers=12)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)
        self.now = 100.0
        self.release_events = []
        self.hosts = []

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()  # A failed assertion must not strand a simulated worker boundary.
        for task in self.hosts:
            if not task.done():
                task.cancel()
        if self.hosts:
            await asyncio.gather(*self.hosts, return_exceptions=True)

    def app(self, *, executor=None, readers=None, **options):
        options.setdefault('tracking', self.tracking)
        self.assertTrue(callable(getattr(admission, "submit_question_admission_async", None)),
                        "Slice1X needs an async-safe submission entry")
        clock = options.pop("clock", lambda: self.now)
        return QuestionHTTPApp(api_token=TOKEN, slots=self.slots, reservations=self.reservations,
                               allowances=self.allowances, executor=executor or self.executor,
                               readers=readers or self.readers, clock=clock, **options)

    async def post(self, application, *, payload=VALID, content=None, headers=None,
                   raise_app_exceptions=False):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application,
                                                                  raise_app_exceptions=raise_app_exceptions),
                                     base_url="http://test") as client:
            return await client.post("/interactions", json=payload if content is None else None,
                                     content=content, headers={"x-api-token": TOKEN}
                                     if headers is None else headers)

    def assert_error(self, response, status, code, message):
        self.assertEqual(response.status_code, status)
        value = response.json()
        self.assertEqual(set(value), {"error"})
        error = value["error"]
        self.assertEqual(set(error), {"code", "message", "request_id"})
        self.assertEqual((error["code"], error["message"]), (code, message))
        UUID(error["request_id"])
        self.assertNotIn("charged", response.text)
        self.assertNotIn("retry-after", response.headers)
        return error

    def assert_joined(self, app):
        for state in app.states:
            self.assertTrue(state["application_done"])
            if state["observer_started"]:
                self.assertTrue(state["observer_done"])

    async def thread_event(self, event):
        self.assertTrue(await anyio.to_thread.run_sync(event.wait, 2))

    def block_read(self, kind="event", readers=None):
        started, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        self.release_events.append(finish)
        def block():
            started.set()
            if not finish.wait(3):
                raise AssertionError("The simulated read was not released")
        (readers or self.readers).hooks[kind] = block
        return started, finish

    def wire(self, *, body=None, headers=None):
        messages = asyncio.Queue()
        messages.put_nowait({"type": "http.request", "body": json.dumps(VALID).encode()
                             if body is None else body, "more_body": False})
        sent = []
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/interactions", "raw_path": b"/interactions", "query_string": b"",
                 "headers": [(b"x-api-token", TOKEN.encode())] if headers is None else headers,
                 "client": ("127.0.0.1", 123), "server": ("test", 80)}
        async def send(message):
            sent.append(message)
        return scope, messages, sent, send

    async def start_wire(self, application, *, send=None):
        scope, messages, sent, default_send = self.wire()
        host = asyncio.create_task(application(scope, messages.get, send or default_send))
        self.hosts.append(host)
        await application.request_started.wait()
        await application.states[-1]["submitted"].wait()
        await application.states[-1]["observer_ready"].wait()
        return host, messages, sent, application.states[-1]

    def assert_key_busy(self):
        lease = self.slots.submit_with_lease(
            InlineExecutor(), lambda owner: self.reservations.reserve(
                owner, ("conversation", CONVERSATION_ID)))
        with self.assertRaises(admission.ReservationConflict):
            lease.result(0)

    async def assert_running_worker_owns_capacity(self):
        finish = threading.Event()
        self.release_events.append(finish)
        leases = [self.slots.submit(self.executor, finish.wait, 3) for _ in range(4)]
        try:
            with self.assertRaises(admission.AdmissionBusy):
                self.slots.submit(InlineExecutor(), lambda: None)
        finally:
            finish.set()
        for lease in leases:
            await lease.wait(2)

    def assert_key_reusable(self):
        lease = self.slots.submit_with_lease(InlineExecutor(), lambda owner: self.reservations.reserve(
            owner, ("conversation", CONVERSATION_ID)))
        self.assertIsNone(lease.result(0))

    async def test_timely_admission_is_fixed_non_success_and_charges_once(self):
        application = self.app()
        response = await self.post(application)
        self.assert_error(response, 503, "interaction_not_ready",
                          "The interaction is not available in this local test.")
        self.assertEqual(sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(10)), 9)
        self.assertTrue(all(self.allowances.try_commit(Allowance.PROFILE_READ) for _ in range(30)))
        self.assert_joined(application)

    async def test_slice3_actual_admission_and_router_reach_only_cleared_internal_evidence(self):
        import httpx2
        from typesafe_sdk import TypeSafeClient, RetryPolicy
        from shopunow.evidence import EvidenceService, ModelBudget
        from shopunow.knowledge import load_knowledge
        from shopunow.workflow import run_incoming
        from tests.test_evidence import EvidenceStorage, EvidenceModels
        from tests.test_incoming import judgment_answers
        from shopunow import knowledge
        catalog=load_knowledge()
        row=next(value for value in catalog.values() if value.subject=='shipping_delivery'
                 and value.classification=='general')
        storage=EvidenceStorage(row,knowledge)
        storage.result['ids']=[[row.source_id]]
        models=EvidenceModels()
        answers=judgment_answers()
        answers['topic_shipping_delivery']['noul']=.9
        observations=[]
        with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
            transport=httpx2.MockTransport(lambda request:httpx2.Response(200,json={
                'model':'offline-jev','usage':{},'answers':answers}))) as client:
            def continuation(*,request_id,**state):
                budget=ModelBudget()
                service=EvidenceService(**state,storage=storage,models=models,
                    tracking=self.tracking,request_id=request_id,created_at='2026-11-12T10:00:00Z',budget=budget)
                return run_incoming(**state,storage=storage,tracking=self.tracking,request_id=request_id,
                    jev_client=client,reviewer_client=None,jev_model='offline-jev',security_trigger=.7,
                    topic_trigger=.5,model_budget=budget,
                    on_cleared=lambda decision:observations.append(service.prepare(decision)))
            application=self.app(on_admitted=continuation)
            with self.assertLogs('shopunow.entry',level='INFO') as captured:
                response=await self.post(application)
        self.assertEqual(response.status_code,503)  # Internal readiness never masquerades as delivered answer.
        self.assertEqual(observations[0].kind,'ready')
        self.assertEqual(observations[0].cleared[0].source_record_id,row.source_id)
        self.assertNotIn(VALID['question'],'\n'.join(captured.output))
        self.assertNotIn(row.answer_facts[0],'\n'.join(captured.output))
        self.assertEqual(storage.calls[:2],['pending_write','pending_read'])
        self.assert_joined(application)

    async def test_confirmed_attack_is_delivered_as_a_typed_result(self):
        from shopunow.schemas import InteractionResponse, ConfirmedAttackOutcome

        def blocked(*, request_id, request, **state):
            return InteractionResponse(user_id=request.user_id,
                conversation_id=request.conversation_id, request_id=request_id,
                outcome=ConfirmedAttackOutcome(kind='confirmed_attack_block',
                                                text='The request cannot be processed.'))

        application = self.app(on_admitted=blocked)
        response = await self.post(application)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['outcome'], {
            'kind':'confirmed_attack_block', 'text':'The request cannot be processed.'})
        self.assertEqual(sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(10)), 9)
        self.assert_joined(application)

    def incoming_fixture(self, **options):
        from tests.incoming_fixture import IncomingFixture
        fixture = IncomingFixture(self.tracking, **options)
        self.addCleanup(fixture.close)
        return fixture

    async def test_native_incoming_attack_has_no_offer_or_conversation_save(self):
        fixture = self.incoming_fixture(verdict='confirmed_attack')
        fixture.answers['prompt_injection']['noul'] = 1.0
        with self.assertLogs('shopunow.entry', level='INFO') as captured:
            response = await self.post(self.app(on_admitted=fixture.continue_admitted),
                                       raise_app_exceptions=True)
        self.assertEqual(response.status_code, 200, (repr(fixture.error), repr(fixture.result), response.text))
        self.assertEqual(response.json()['outcome']['kind'], 'confirmed_attack_block')
        self.assertEqual([kind for kind, _ in fixture.calls], ['judge', 'review'])
        self.assertEqual(fixture.storage_calls, [])  # No pending, marker, offer or retrieval operation.
        events = [json.loads(record.getMessage()) for record in captured.records]
        stages = {event['event'] for event in events}
        self.assertTrue({'incoming_judgment','incoming_gate'} <= stages)
        for event in events:
            self.assertTrue(set(event) <= {'utc_time','event','request_id','outcome','http_status'})
        serialized = json.dumps(events)
        for private in (VALID['question'], USER_ID, CONVERSATION_ID, 'offline-test'):
            self.assertNotIn(private, serialized)

    async def test_native_uncertainty_displays_only_exact_verified_offer_and_marker(self):
        fixture = self.incoming_fixture()
        fixture.answers['prompt_injection']['noul'] = 0.9
        response = await self.post(self.app(on_admitted=fixture.continue_admitted))
        self.assertEqual(response.status_code, 200)
        outcome = response.json()['outcome']
        self.assertEqual(outcome['kind'], 'fallback_error')
        self.assertIsNotNone(outcome['offer_id'])
        self.assertEqual([(name, operation) for name, operation, _ in fixture.storage_calls],
            [('offers','add'), ('offers','get'), ('conversations','add'),
             ('conversations','get'), ('offers','get')])
        self.assertNotIn(VALID['question'], json.dumps(fixture.storage_calls))
        self.assertFalse(self.tracking.excludes((CONVERSATION_ID,)))
        # Reopen through the actual transcript verifier and public projector: notice only.
        from shopunow.records import verify_complete_transcript
        from shopunow.schemas import HistorySelectors
        verified = verify_complete_transcript(header_result(), fixture.conversations.raw(),
            user_id=USER_ID, conversation_id=CONVERSATION_ID,
            offer_results={outcome['offer_id']:fixture.offers.raw()}).record
        self.assertIsNotNone(verified)
        history = api.project_history_response(verified, selectors=HistorySelectors(
            request_id=response.json()['request_id'], user_id=USER_ID,
            conversation_id=CONVERSATION_ID))
        self.assertEqual(history.messages, ())
        self.assertEqual(len(history.notices), 1)
        self.assertEqual(history.notices[0].offer_id, outcome['offer_id'])

    async def test_incoming_refuses_mismatched_tracker_before_any_model_or_write(self):
        fixture = self.incoming_fixture(answers={})
        fixture.tracking = admission.OwnedRecordTracking()  # Erroneous application wiring.
        response = await self.post(self.app(on_admitted=fixture.continue_admitted))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.storage_calls, [])

    async def test_prepared_attack_cannot_pass_a_new_conversation_hold(self):
        fixture = self.incoming_fixture(verdict='confirmed_attack')
        fixture.answers['prompt_injection']['noul'] = 0.9
        async def hold_after_serialization(state):
            self.tracking.exclude_conversation(CONVERSATION_ID)
        response = await self.post(self.app(on_admitted=fixture.continue_admitted,
                                            before_response_start=hold_after_serialization))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'conversation_unresolved')
        self.assertNotIn('outcome', response.json())

    async def test_prepared_result_is_suppressed_after_serialization_expiry_or_withdrawal(self):
        for reason in ('expiry', 'withdrawal'):
            with self.subTest(reason=reason):
                self.now = 100.0
                fixture = self.incoming_fixture(verdict='confirmed_attack')
                fixture.answers['prompt_injection']['noul'] = 0.9
                async def stop_after_serialization(state):
                    if reason == 'expiry':
                        self.now = 190.0
                    else:
                        await api.stop_question_admission(state['handle'])
                response = await self.post(self.app(on_admitted=fixture.continue_admitted,
                    before_response_start=stop_after_serialization))
                self.assertEqual(response.status_code, 503)  # Preserve the approved timeout envelope.
                self.assertEqual(response.json()['error']['code'],
                    'request_timeout' if reason == 'expiry' else 'request_stopped')
                self.assertNotIn('outcome', response.json())

    async def test_incoming_disabled_without_triggers_starts_no_model_or_storage(self):
        fixture = self.incoming_fixture(trigger=None)
        response = await self.post(self.app(on_admitted=fixture.continue_admitted))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'interaction_not_ready')
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.storage_calls, [])

    async def test_malformed_judgments_use_verified_content_free_fallback(self):
        fixture = self.incoming_fixture(answers={})
        response = await self.post(self.app(on_admitted=fixture.continue_admitted))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()['outcome']['offer_id'])
        self.assertEqual([name for name, _ in fixture.calls], ['judge'])
        marker = fixture.conversations.raw()
        self.assertEqual(marker['documents'], ['preclearance_terminal'])
        self.assertEqual(marker['metadatas'][0]['reason_code'], 'dependency_failure')
        self.assertNotIn(VALID['question'], json.dumps(fixture.storage_calls))

    async def test_unknown_offer_write_holds_actual_admission_tracker_and_blocks_next_question(self):
        fixture = self.incoming_fixture(answers={})
        def uncertain_write(name, operation):
            if operation == 'add':
                raise TimeoutError('Controlled possible remote commit.')
        fixture.storage_hook = uncertain_write
        application = self.app(on_admitted=fixture.continue_admitted)
        first = await self.post(application)
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json()['error']['code'], 'conversation_unresolved')
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))
        before = (len(fixture.calls), len(fixture.storage_calls))
        second = await self.post(application)
        self.assertEqual(second.status_code, 409)
        self.assertEqual((len(fixture.calls), len(fixture.storage_calls)), before)
        self.assertEqual(sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(10)), 9)

    async def test_customer_restricted_topics_and_old_conversations_do_no_downstream_work(self):
        fixture = self.incoming_fixture()
        fixture.answers['topic_hr']['noul'] = 0.9
        response = await self.post(self.app(on_admitted=fixture.continue_admitted))
        # Slice 2 retains an internal refusal; later slices own its verified terminal delivery.
        self.assertEqual(response.status_code, 503)
        self.assertEqual([name for name, _ in fixture.calls], ['judge'])
        self.assertEqual(fixture.storage_calls, [])  # No employee knowledge or handoff access.
        old = await self.post(self.app(on_admitted=fixture.continue_admitted,
                                      tracking=admission.OwnedRecordTracking()))
        self.assertEqual(old.status_code, 409)
        self.assertEqual(old.json()['error']['code'], 'conversation_read_only')
        self.assertEqual(len(fixture.calls), 1)

    async def test_wrong_interaction_request_reference_is_never_displayed(self):
        from tests.test_records import REQUEST_ID
        fixture = self.incoming_fixture(verdict='confirmed_attack')
        fixture.answers['prompt_injection']['noul'] = 0.9
        def wrong_reference(*, request_id, **state):
            return fixture.continue_admitted(request_id=REQUEST_ID, **state)
        response = await self.post(self.app(on_admitted=wrong_reference))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['error']['code'], 'technical_error')
        self.assertNotIn('outcome', response.json())

    async def test_disconnect_during_fallback_write_retains_worker_capacity_and_reservation(self):
        fixture = self.incoming_fixture(answers={})
        started, finish = threading.Event(), threading.Event()
        self.release_events.append(finish)
        def blocked_marker(name, operation):
            if name == 'conversations' and operation == 'add':
                started.set()
                if not finish.wait(3):
                    raise AssertionError('Controlled marker write was not released.')
        fixture.storage_hook = blocked_marker
        application = self.app(on_admitted=fixture.continue_admitted)
        host, messages, sent, state = await self.start_wire(application)
        await self.thread_event(started)
        self.assert_key_busy()
        await self.assert_running_worker_owns_capacity()
        messages.put_nowait({'type':'http.disconnect'})
        with anyio.fail_after(2):
            await host  # HTTP cleanup does not claim the remote write stopped.
        self.assertEqual(sent, [])
        self.assertFalse(state['handle'].lease.capacity_released)
        self.assert_key_busy()
        finish.set()
        result = await state['handle'].lease.wait(2)
        self.assertIsNone(result.interaction)
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))
        self.assertEqual([(name, op) for name, op, _ in fixture.storage_calls],
                         [('offers','add'),('offers','get'),('conversations','add')])
        self.assert_key_reusable()

    async def test_failed_final_tracking_check_returns_fixed_error_with_deadline_precedence(self):
        from tests.verified_creation import verified_tracking
        test_case = self
        class UnreliableTracking(admission.OwnedRecordTracking):
            fail = False
            expire = False
            def _excludes_locked(self, ids):
                if self.fail:
                    if self.expire:
                        test_case.now = 190.0  # Failure after waiting can consume the budget.
                    raise RuntimeError('Private tracker failure.')
                return super()._excludes_locked(ids)
        for expire in (False, True):
            with self.subTest(expire=expire):
                self.now = 100.0
                self.tracking = verified_tracking(UnreliableTracking())
                fixture = self.incoming_fixture(verdict='confirmed_attack')
                fixture.answers['prompt_injection']['noul'] = 0.9
                async def fail_after_serialization(state):
                    self.tracking.fail, self.tracking.expire = True, expire
                response = await self.post(self.app(on_admitted=fixture.continue_admitted,
                    before_response_start=fail_after_serialization))
                self.assertEqual(response.status_code, 503 if expire else 500)
                self.assertEqual(response.json()['error']['code'],
                                 'request_timeout' if expire else 'technical_error')
                self.assertNotIn('Private tracker failure', response.text)

    async def test_known_hold_cannot_replace_foreign_conversation_ownership_error(self):
        self.tracking.exclude_conversation(CONVERSATION_ID)
        self.readers.header_value = header_result({**HEADER, 'owner_user_id':'syn-cust-02'})
        self.readers.header_value['metadatas'][0]['owner_user_id'] = 'syn-cust-02'
        response = await self.post(self.app())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], 'ownership_failure')

    async def test_validation_precedes_capacity_and_never_starts_readers(self):
        application = self.app()
        for content, headers, status, code, message in [
            (b"{" * 32769, {}, 401, "unauthorized", "The API token is invalid."),
            (b"{" * 32769, {"x-api-token": TOKEN}, 413, "body_too_large", "The request body is too large."),
            (b"{", {"x-api-token": TOKEN}, 422, "invalid_request", "The request is invalid."),
        ]:
            self.assert_error(await self.post(application, content=content, headers=headers),
                              status, code, message)
        self.assertEqual(self.readers.calls, [])
        self.assert_joined(application)

    async def test_absent_and_valid_foreign_headers_are_wire_equivalent(self):
        foreign = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
        foreign["metadatas"][0]["owner_user_id"] = "syn-cust-02"
        envelopes = []
        for raw in [event_result(), foreign]:
            readers = SimulatedReaders()
            readers.header_value = raw
            application = self.app(readers=readers)
            error = self.assert_error(await self.post(application), 404, "ownership_failure",
                                      "The conversation is unavailable.")
            envelopes.append({k: v for k, v in error.items() if k != "request_id"})
            self.assert_joined(application)
        self.assertEqual(envelopes[0], envelopes[1])

    async def test_profile_corrupt_prefix_and_quota_mappings(self):
        for kind, raw, status, code, message in [
            ("profile", event_result(), 404, "profile_unavailable", "The profile is unavailable."),
            ("header", {"ids": ["wrong"], "documents": [], "metadatas": []}, 409,
             "conversation_unresolved", "The conversation state could not be verified."),
            ("event", event_result(transcript_pair()[0]), 409, "conversation_unresolved",
             "The conversation state could not be verified."),
        ]:
            readers = SimulatedReaders()
            setattr(readers, f"{kind}_value", raw)
            self.assert_error(await self.post(self.app(readers=readers)), status, code, message)
        application = self.app()
        for _ in range(10):
            self.assert_error(await self.post(application), 503, "interaction_not_ready",
                              "The interaction is not available in this local test.")
        self.assert_error(await self.post(application), 429, "question_limit",
                          "The question allowance is exhausted.")
        self.assertEqual(sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(10)), 0)
        self.assertTrue(all(self.allowances.try_commit(Allowance.PROFILE_READ) for _ in range(30)))
        self.assert_joined(application)

    async def test_48_49_50_51_event_boundaries_validate_offers_before_full(self):
        for count, status, code, message in [
            (48, 503, "interaction_not_ready", "The interaction is not available in this local test."),
            (49, 409, "conversation_full", "This conversation is full; start a new conversation."),
            (50, 409, "conversation_full", "This conversation is full; start a new conversation."),
            (51, 409, "conversation_unresolved", "The conversation state could not be verified."),
        ]:
            readers = SimulatedReaders()
            readers.event_value = event_result(*(transcript_marker(i) for i in range(1, count + 1)))
            readers.offers = {str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain")
                              for i in range(1, count + 1)}
            self.assert_error(await self.post(self.app(readers=readers)), status, code, message)
            self.assertEqual(sum(c[0] == "offer" for c in readers.calls), count if count <= 50 else 0)

    async def test_normally_served_app_has_no_interactions_route(self):
        self.assert_error(await self.post(api.create_app()), 404, "not_found",
                          "The operation was not found.")

    async def test_execution_capacity_rejects_without_readers_or_charge(self):
        finish = threading.Event()
        self.release_events.append(finish)
        leases = [self.slots.submit(self.executor, finish.wait, 3) for _ in range(5)]
        application = self.app()
        self.assert_error(await self.post(application), 503, "execution_capacity",
                          "Execution capacity is unavailable.")
        self.assertEqual(self.readers.calls, [])
        self.assertEqual(sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(10)), 10)
        self.assert_joined(application)
        finish.set()
        for lease in leases:
            await lease.wait(2)

    async def test_disconnect_stops_http_tasks_but_retains_running_worker_key(self):
        started, finish = self.block_read()
        application = self.app()
        host, messages, sent, state = await self.start_wire(application)
        await self.thread_event(started)
        messages.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(application)
        self.assert_key_busy()
        await self.assert_running_worker_owns_capacity()
        self.assertFalse(state["handle"].lease.cancel())
        self.assertEqual([c[0] for c in self.readers.calls], ["profile", "header", "event"])
        finish.set()
        result = await state["handle"].lease.wait(2)
        self.assertEqual((result.status, result.charged), ("stopped", False))
        self.assert_key_reusable()

    async def test_direct_cancel_host_joins_observer_without_joining_business_read(self):
        started, finish = self.block_read()
        application = self.app()
        host, messages, sent, state = await self.start_wire(application)
        await self.thread_event(started)
        host.cancel()  # Target the actual server-facing boundary, never its private child.
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(application)
        self.assert_key_busy()
        finish.set()
        self.assertEqual((await state["handle"].lease.wait(2)).status, "stopped")

    async def test_send_oserror_is_sanitized_suppresses_further_sends_and_joins(self):
        application = self.app()
        calls = []
        async def broken_send(message):
            calls.append(message)
            raise OSError("private-contact@example.test")
        scope, messages, sent, _ = self.wire()
        with self.assertLogs("shopunow.entry", level="INFO") as captured:
            await application(scope, messages.get, broken_send)
        self.assertEqual(len(calls), 1)
        self.assertTrue(application.states[0]["closed"])
        self.assert_joined(application)
        text = " ".join(captured.output)
        self.assertIn("delivery_failure", text)
        self.assertNotIn("private-contact", text)
        self.assertNotIn(VALID["question"], text)

    async def test_observer_lives_through_blocked_send_then_disconnect_suppresses_body(self):
        application = self.app()
        sending = anyio.Event()
        sent = []
        async def blocked_send(message):
            sent.append(message)
            sending.set()
            await anyio.sleep_forever()  # Native cancellation must interrupt this send.
        host, messages, _, state = await self.start_wire(application, send=blocked_send)
        await sending.wait()
        self.assertFalse(state["observer_done"])
        messages.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(host, 2)
        self.assertEqual([m["type"] for m in sent], ["http.response.start"])
        self.assert_joined(application)

    async def test_anyio_scope_cancel_joins_http_children_and_retains_running_read(self):
        started, finish = self.block_read()
        application = self.app()
        scope, messages, sent, send = self.wire()
        entered = anyio.Event()
        done = anyio.Event()
        cancel_scopes = []
        async def server_host():
            try:
                with anyio.CancelScope() as cancel_scope:
                    cancel_scopes.append(cancel_scope)
                    entered.set()
                    await application(scope, messages.get, send)
            finally:
                done.set()
        async with anyio.create_task_group() as group:
            group.start_soon(server_host)
            await entered.wait()
            await self.thread_event(started)
            cancel_scopes[0].cancel()
            await done.wait()
        self.assertTrue(cancel_scopes[0].cancelled_caught)
        self.assertEqual(sent, [])
        self.assert_joined(application)
        self.assert_key_busy()
        finish.set()
        await application.states[0]["handle"].lease.wait(2)

    async def test_repeated_direct_cancel_during_cleanup_thread_limiter_wait_is_joined(self):
        application = self.app()
        sending = anyio.Event()
        async def blocked_send(message):
            sending.set()
            await anyio.sleep_forever()
        host, _, sent, state = await self.start_wire(application, send=blocked_send)
        await sending.wait()
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()  # Force cleanup to wait before dispatching a control thread.
        try:
            host.cancel()
            await state["cleanup_started"].wait()
            host.cancel()
            host.cancel()  # Repeated raw cancellation targets only the retained server-facing task.
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
        finally:
            limiter.release()
            limiter.total_tokens = original_tokens
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(host, 2)
        self.assert_joined(application)
        self.assertEqual(sent, [])

    async def test_direct_cancel_while_snapshot_waits_for_thread_token_keeps_handle_owned(self):
        started, finish = self.block_read()
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()
        application = self.app()
        try:
            host, _, sent, state = await self.start_wire(application)
            self.assertTrue(started.wait(2))  # Dedicated business executor ignores AnyIO tokens.
            host.cancel()
            host.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
            self.assertIsNotNone(state["handle"])
        finally:
            limiter.release()
            limiter.total_tokens = original_tokens
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(application)
        self.assert_key_busy()
        finish.set()
        await state["handle"].lease.wait(2)

    async def test_queued_prestart_cancel_prevents_all_readers_and_releases_capacity(self):
        queued_executor = ObservedThreadPool(max_workers=1)
        self.addCleanup(queued_executor.shutdown, wait=True, cancel_futures=True)
        finish = threading.Event()
        self.release_events.append(finish)
        occupied = queued_executor.submit(finish.wait, 3)
        application = self.app(executor=queued_executor)
        host, _, sent, state = await self.start_wire(application)
        host.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(host, 2)
        self.assertTrue(state["handle"].lease.cancel())
        self.assertEqual(self.readers.calls, [])
        self.assertEqual(sent, [])
        self.assert_joined(application)
        finish.set()
        await anyio.to_thread.run_sync(occupied.result, 2)

    async def test_expiry_retains_running_read_and_key_while_independent_question_continues(self):
        from tests.verified_creation import verified_tracking
        verified_tracking(self.tracking, conversation_id=str(UUID(int=999)))
        started, finish = self.block_read()
        elapsed = [None]
        old_block = self.readers.hooks["event"]
        def expire_soon():
            elapsed[0] = monotonic() - 14.99
            old_block()
        self.readers.hooks["event"] = expire_soon
        def clock():
            return 100.0 if elapsed[0] is None else 100.0 + monotonic() - elapsed[0]
        application = self.app(clock=clock)
        with self.assertLogs("shopunow.entry", level="INFO") as captured:
            response_task = asyncio.create_task(self.post(application))
            self.hosts.append(response_task)
            await self.thread_event(started)
            response = await asyncio.wait_for(response_task, 2)
            self.assert_error(response, 503, "request_timeout", "The request timed out.")
            self.assert_key_busy()
            await self.assert_running_worker_owns_capacity()
            duplicate = await self.post(self.app(readers=SimulatedReaders()))
            self.assert_error(duplicate, 409, "conversation_busy", "The conversation is busy.")
            independent = SimulatedReaders()
            other_id = str(UUID(int=999))
            independent.header_value = header_result({**HEADER, "conversation_id": other_id})
            independent.header_value["ids"] = ["conversation_header:000000000000000000000000000003e7"]
            independent.header_value["metadatas"][0]["conversation_id"] = other_id
            other = await self.post(self.app(readers=independent),
                                    payload={**VALID, "conversation_id": other_id})
            self.assert_error(other, 503, "interaction_not_ready",
                              "The interaction is not available in this local test.")
            finish.set()
            await application.states[0]["handle"].lease.wait(2)
            self.assert_key_reusable()
        self.assert_joined(application)
        events = [json.loads(record.getMessage()) for record in captured.records]
        reference = response.json()["error"]["request_id"]
        stages = [event["event"] for event in events if event["request_id"] == reference]
        self.assertIn("question_terminal", stages)
        self.assertIn("question_completion", stages)
        self.assertLess(stages.index("question_terminal"), stages.index("question_completion"))

    async def test_unknown_hit_and_unexpected_reader_failure_are_safe_technical_errors(self):
        with patch.object(self.allowances, "_try_commit_locked", side_effect=RuntimeError("private-hit")):
            application = self.app()
            response = await self.post(application)
            self.assert_error(response, 500, "technical_error", "The operation failed.")
            self.assertIsNone(application.states[0]["handle"].attempt.charged)
        def fail_read():
            raise RuntimeError("private-read")
        self.readers.hooks["profile"] = fail_read
        self.assert_error(await self.post(self.app()), 500, "technical_error", "The operation failed.")

    async def test_late_hit_crossing_predeadline_keeps_admission_time_and_posthit_deadline(self):
        native_hit = self.allowances._try_commit_locked
        def late_hit(kind):
            self.now = 116.0  # The eligible 100-second sample is held before this boundary.
            return native_hit(kind)
        application = self.app()
        with patch.object(self.allowances, "_try_commit_locked", side_effect=late_hit):
            self.assert_error(await self.post(application), 503, "interaction_not_ready",
                              "The interaction is not available in this local test.")
        attempt = application.states[0]["handle"].attempt
        self.assertEqual(attempt.admission.admitted_at, 100.0)
        self.assertEqual(attempt.admission.question_deadline, 190.0)
        self.assertTrue(attempt.charged)

    async def test_expired_posthit_retains_charge_and_returns_timeout(self):
        native_hit = self.allowances._try_commit_locked
        def late_hit(kind):
            self.now = 190.0
            return native_hit(kind)
        application = self.app()
        with patch.object(self.allowances, "_try_commit_locked", side_effect=late_hit):
            self.assert_error(await self.post(application), 503, "request_timeout", "The request timed out.")
        self.assertTrue(application.states[0]["handle"].attempt.charged)

    async def test_hit_lock_wait_keeps_event_loop_responsive_and_repeated_cancel_owned(self):
        hit_entered = anyio.Event()
        finish = threading.Event()
        self.release_events.append(finish)
        loop = asyncio.get_running_loop()
        native_hit = self.allowances._try_commit_locked
        def blocked_hit(kind):
            loop.call_soon_threadsafe(hit_entered.set)
            if not finish.wait(3):
                raise AssertionError("Hit was not released")
            return native_hit(kind)
        application = self.app()
        with patch.object(self.allowances, "_try_commit_locked", side_effect=blocked_hit):
            host, _, sent, state = await self.start_wire(application)
            await hit_entered.wait()
            progressed = anyio.Event()
            asyncio.get_running_loop().call_soon(progressed.set)
            await progressed.wait()  # A direct attempt property read on the loop would deadlock here.
            host.cancel()
            host.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
            self.assert_key_busy()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(application)
        self.assertTrue(state["handle"].attempt.charged)
        self.assertFalse(state["handle"].attempt.authorize_normal_work())

    async def test_dispatch_failure_after_hit_started_keeps_loop_and_cleanup_owned(self):
        entered = threading.Event()
        finish = threading.Event()
        self.release_events.append(finish)
        native_hit = self.allowances._try_commit_locked
        def blocked_hit(kind):
            entered.set()
            if not finish.wait(3):
                raise AssertionError("Dispatch-failure hit was not released")
            return native_hit(kind)
        application = self.app(executor=SubmitThenRaise(self.executor, started=entered))
        scope, messages, sent, send = self.wire()
        with patch.object(self.allowances, "_try_commit_locked", side_effect=blocked_hit):
            host = asyncio.create_task(application(scope, messages.get, send))
            self.hosts.append(host)
            await application.request_started.wait()
            await self.thread_event(entered)
            progressed = anyio.Event()
            asyncio.get_running_loop().call_soon(progressed.set)
            await progressed.wait()
            self.assert_key_busy()
            host.cancel()
            host.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(application)

    async def test_disconnect_during_incomplete_body_has_no_dispatch_or_second_reader(self):
        application = self.app()
        scope, messages, sent, send = self.wire()
        messages.get_nowait()
        messages.put_nowait({"type": "http.request", "body": b'{"user_id":', "more_body": True})
        messages.put_nowait({"type": "http.disconnect"})
        with self.assertRaises(ClientDisconnect):
            await application(scope, messages.get, send)
        self.assertEqual(sent, [])
        self.assertEqual(self.readers.calls, [])
        self.assertIsNone(application.states[0]["handle"])
        self.assertFalse(application.states[0]["observer_started"])
        self.assert_joined(application)
        self.assertEqual(sum(self.allowances.try_commit(Allowance.QUESTION) for _ in range(10)), 10)

    async def test_normal_rejected_and_failed_application_events_are_safe_and_correlated(self):
        application = self.app()
        def fail():
            raise RuntimeError("private-contact@example.test")
        with self.assertLogs("shopunow.entry", level="INFO") as captured:
            normal = await self.post(application)
            rejected = await self.post(application, headers={"x-api-token": "wrong-private-token"})
            self.readers.hooks["profile"] = fail
            failed = await self.post(application)
        events = [json.loads(record.getMessage()) for record in captured.records]
        references = {r.json()["error"]["request_id"] for r in [normal, rejected, failed]}
        self.assertEqual(len(references), 3)
        self.assertEqual({event["request_id"] for event in events}, references)
        self.assertIn("admitted", [event["outcome"] for event in events])
        self.assertIn("technical_error", [event["outcome"] for event in events])
        for event in events:
            self.assertLessEqual(set(event), {"utc_time", "event", "request_id", "outcome", "http_status"})
            UUID(event["request_id"])
        text = " ".join(captured.output) + normal.text + rejected.text + failed.text
        for sentinel in [TOKEN, "wrong-private-token", VALID["question"], USER_ID,
                         CONVERSATION_ID, "private-contact", "Traceback", "charged"]:
            self.assertNotIn(sentinel, text)
        self.assert_joined(application)

    async def test_actual_completed_results_recheck_current_withdrawal_and_deadline(self):
        native_state = admission.QuestionAttempt.http_state
        loop = asyncio.get_running_loop()
        for unknown_hit in [False, True]:
            for stop in ["withdrawal", "deadline"]:
                with self.subTest(unknown_hit=unknown_hit, stop=stop):
                    self.now = 100.0
                    entered = anyio.Event()
                    finish = threading.Event()
                    self.release_events.append(finish)
                    def delayed_state(attempt, result):
                        loop.call_soon_threadsafe(entered.set)
                        if not finish.wait(3):
                            raise AssertionError("Final selection was not released")
                        return native_state(attempt, result)
                    application = self.app(executor=InlineExecutor())
                    native_hit = self.allowances._try_commit_locked
                    def hit(kind):
                        if unknown_hit:
                            raise RuntimeError("private unknown hit")
                        return native_hit(kind)
                    with patch.object(admission.QuestionAttempt, "http_state", delayed_state), \
                         patch.object(self.allowances, "_try_commit_locked", side_effect=hit):
                        response_task = asyncio.create_task(self.post(application))
                        self.hosts.append(response_task)
                        await entered.wait()
                        handle = application.states[0]["handle"]
                        if stop == "withdrawal":
                            await api.stop_question_admission(handle)
                            code, message = "request_stopped", "The request stopped."
                        else:
                            self.now = 115.0 if unknown_hit else 190.0
                            code, message = "request_timeout", "The request timed out."
                        finish.set()
                        response = await asyncio.wait_for(response_task, 2)
                    self.assert_error(response, 503, code, message)
                    self.assert_joined(application)

    async def test_prephase_wait_expiry_rechecks_hit_lock_and_uses_won_ninety_seconds(self):
        read_started, finish_read = self.block_read()
        old_read = self.readers.hooks["event"]
        def read_near_deadline():
            self.now = 114.0
            old_read()
        self.readers.hooks["event"] = read_near_deadline
        first_snapshot, second_snapshot, hit_entered = anyio.Event(), anyio.Event(), anyio.Event()
        finish_hit = threading.Event()
        self.release_events.append(finish_hit)
        loop = asyncio.get_running_loop()
        native_state = admission.QuestionAttempt.http_state
        native_hit = self.allowances._try_commit_locked
        checks = [0]
        def observe_state(attempt, result):
            checks[0] += 1
            if checks[0] == 1:
                if not read_started.wait(2):
                    raise AssertionError("The transcript read did not reach its barrier")
                state = native_state(attempt, result)
                loop.call_soon_threadsafe(first_snapshot.set)
                return state
            loop.call_soon_threadsafe(second_snapshot.set)
            return native_state(attempt, result)
        def blocked_hit(kind):
            loop.call_soon_threadsafe(hit_entered.set)
            if not finish_hit.wait(3):
                raise AssertionError("Late eligible hit was not released")
            return native_hit(kind)
        application = self.app()
        with patch.object(admission.QuestionAttempt, "http_state", observe_state), \
             patch.object(self.allowances, "_try_commit_locked", side_effect=blocked_hit):
            response_task = asyncio.create_task(self.post(application))
            self.hosts.append(response_task)
            await first_snapshot.wait()
            finish_read.set()
            await hit_entered.wait()
            self.now = 116.0
            await asyncio.wait_for(second_snapshot.wait(), 2)  # Observe the native one-second wait expiry.
            self.assertFalse(response_task.done())
            finish_hit.set()
            response = await asyncio.wait_for(response_task, 2)
        self.assert_error(response, 503, "interaction_not_ready",
                          "The interaction is not available in this local test.")
        attempt = application.states[0]["handle"].attempt
        self.assertEqual(attempt.admission.admitted_at, 114.0)
        self.assertEqual(attempt.admission.question_deadline, 204.0)
        self.assertTrue(attempt.charged)
        self.assert_joined(application)

    async def test_dispatch_error_after_started_worker_keeps_correlated_finish_event(self):
        entered, finish = threading.Event(), threading.Event()
        self.release_events.append(finish)
        native_hit = self.allowances._try_commit_locked
        def blocked_hit(kind):
            entered.set()
            if not finish.wait(3):
                raise AssertionError("The uncertain-dispatch worker was not released")
            return native_hit(kind)
        application = self.app(executor=SubmitThenRaise(self.executor, started=entered))
        scope, messages, sent, send = self.wire()
        with self.assertLogs("shopunow.entry", level="INFO") as captured, \
             patch.object(self.allowances, "_try_commit_locked", side_effect=blocked_hit):
            host = asyncio.create_task(application(scope, messages.get, send))
            self.hosts.append(host)
            await application.request_started.wait()
            await self.thread_event(entered)
            finish.set()
            with self.assertRaisesRegex(RuntimeError, "dispatch outcome uncertain"):
                await asyncio.wait_for(host, 2)
            await anyio.to_thread.run_sync(self.executor.dispatched[0].result, 2)
        self.assertEqual(sent[0]["status"], 500)
        envelope = json.loads(sent[1]["body"])
        reference = envelope["error"]["request_id"]
        events = [json.loads(record.getMessage()) for record in captured.records]
        self.assertTrue(any(event["event"] == "question_worker_finished" and
                            event["request_id"] == reference for event in events))
        self.assertNotIn("dispatch outcome uncertain", " ".join(captured.output))
        self.assert_joined(application)

    async def test_application_exception_after_observer_start_keeps_native_safe_handler_and_joins(self):
        application = self.app()
        scope, messages, sent, send = self.wire()
        with patch.object(api, "question_admission_response", side_effect=RuntimeError("private-response")):
            with self.assertRaisesRegex(RuntimeError, "private-response"):
                await application(scope, messages.get, send)
        self.assertEqual(sent[0]["status"], 500)
        self.assertEqual(json.loads(sent[1]["body"])["error"]["code"], "technical_error")
        self.assertNotIn("private-response", str(sent))
        self.assertTrue(application.states[0]["observer_started"])
        self.assert_joined(application)

    async def test_worker_telemetry_failure_cannot_change_admission_or_native_completion(self):
        observed = []
        def broken_observer(result):
            observed.append(result.status)
            raise RuntimeError("private-sink")
        handle = await admission.submit_question_admission_async(
            VALID, slots=self.slots, reservations=self.reservations, allowances=self.allowances,
            executor=self.executor, readers=self.readers, clock=lambda: self.now,
            on_worker_finished=broken_observer,
            tracking=self.tracking,
        )
        result = await handle.lease.wait(2)
        self.assertEqual((result.status, result.charged), ("admitted", True))
        self.assertEqual(observed, ["admitted"])
        self.assert_key_reusable()


# Flow: complete literal fixtures -> real FastAPI/admission -> assert fixed wire output,
# quota effects and owned task completion; direct ASGI cases add lifecycle evidence below.
