# Python 3.12 / FastAPI 0.143.2; local injected API, controlled external reads only.
import asyncio
import json
import threading
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import UUID
import anyio
import httpx
import fastapi.routing
from limits.storage import MemoryStorage
from shopunow import admission, api
from shopunow.allowances import Allowance, RollingAllowances
from tests.test_admission import InlineExecutor, ObservedThreadPool
from tests.test_identity_reads import IdentityReaders, EMPTY, preference_result
from tests.test_records import USER_ID

TOKEN = 'local-identity-token'


class IdentityHTTPHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.slots = admission.ExecutionSlots()
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.allowances = RollingAllowances(self.storage, threading.Lock())
        self.readers = IdentityReaders()
        self.executor = ObservedThreadPool(max_workers=12)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)
        self.now = 100.0
        self.release_events, self.hosts = [], []

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        for task in self.hosts:
            if not task.done():
                task.cancel()
        if self.hosts:
            await asyncio.gather(*self.hosts, return_exceptions=True)

    def app(self, **options):
        self.assertTrue(callable(getattr(api, 'identity_read_response', None)),
                        'Missing complete identity HTTP integration')
        from tests.identity_http_app import IdentityReadHTTPApp
        return IdentityReadHTTPApp(api_token=TOKEN, slots=self.slots, allowances=self.allowances,
            executor=options.pop('executor', self.executor), readers=self.readers,
            roster=frozenset({USER_ID}), projector=options.pop('projector', api.project_identity_read),
            clock=lambda: self.now, **options)

    async def get(self, app, *, operation='preference', headers=None, params=None, content=None):
        method, path = ('GET', '/preferences') if operation == 'preference' else ('POST', '/profiles/resolve')
        if content is None:
            content = b'' if operation == 'preference' else json.dumps({'user_id': USER_ID}).encode()
        if params is None:
            params = {'user_id': USER_ID} if operation == 'preference' else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url='http://test') as client:
            return await client.request(method, path, params=params, content=content,
                headers={'x-api-token': TOKEN} if headers is None else headers)

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        payload = response.json()
        self.assertEqual(set(payload), {'error'})
        self.assertEqual(set(payload['error']), {'code', 'message', 'request_id'})
        self.assertEqual(payload['error']['code'], code)
        UUID(payload['error']['request_id'])
        self.assertNotIn('retry-after', response.headers)

    def assert_joined(self, application):
        for state in application.states:
            self.assertTrue(state['application_done'])
            if state['observer_started']:
                self.assertTrue(state['observer_done'])

    def remaining(self):
        return sum(self.allowances.try_commit(Allowance.PROFILE_READ) for _ in range(31))

    async def thread_event(self, event):
        self.assertTrue(await anyio.to_thread.run_sync(event.wait, 2))

    def block_read(self):
        started, finish = threading.Event(), threading.Event()
        self.release_events.append(finish)
        def blocked():
            started.set()
            if not finish.wait(3):
                raise AssertionError('Controlled read was not released')
        self.readers.hooks['profile'] = blocked
        return started, finish

    def wire(self):
        messages = asyncio.Queue()
        messages.put_nowait({'type': 'http.request', 'body': b'', 'more_body': False})
        sent = []
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'GET', 'scheme': 'http', 'path': '/preferences',
                 'raw_path': b'/preferences', 'query_string': f'user_id={USER_ID}'.encode(),
                 'headers': [(b'x-api-token', TOKEN.encode())],
                 'client': ('127.0.0.1', 123), 'server': ('test', 80)}
        async def send(message):
            sent.append(message)
        return scope, messages, sent, send

    async def start_wire(self, application, *, send=None):
        scope, messages, sent, default_send = self.wire()
        task = asyncio.create_task(application(scope, messages.get, send or default_send))
        self.hosts.append(task)
        await application.request_started.wait()
        state = application.states[-1]
        await state['submitted'].wait()
        await state['observer_ready'].wait()
        return task, messages, sent, state

    async def assert_running_capacity_retained(self):
        finish = threading.Event()
        self.release_events.append(finish)
        others = [self.slots.submit(self.executor, finish.wait, 3) for _ in range(4)]
        try:
            with self.assertRaises(admission.AdmissionBusy):
                self.slots.submit(InlineExecutor(), lambda: None)
        finally:
            finish.set()
        for lease in others:
            await lease.wait(2)

class IdentityHTTPTests(IdentityHTTPHarness):
    async def test_both_operations_return_native_bound_success_and_join(self):
        app = self.app()
        for operation in ['profile', 'preference']:
            result = await self.get(app, operation=operation)
            self.assertEqual(result.status_code, 200)
            body = result.json()
            UUID(body['request_id'])
            self.assertEqual(body['user_id'], USER_ID)
            if operation == 'profile':
                self.assertEqual(body['profile'], {'user_id': USER_ID, 'role': 'customer',
                                                   'home_department': None})
                self.assertIs(body['created'], False)
                self.assertEqual(set(body), {'request_id', 'user_id', 'profile', 'created'})
            else:
                self.assertEqual(set(body), {'request_id', 'user_id', 'response_style'})
                self.assertIsNone(body['response_style'])
        self.assertEqual(self.remaining(), 28)
        self.assert_joined(app)

    async def test_profile_client_selection_never_replaces_stored_binding(self):
        app = self.app()
        result = await self.get(app, operation='profile', content=json.dumps({
            'user_id': USER_ID, 'role': 'employee', 'home_department': 'hr'}).encode())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['profile']['role'], 'customer')
        self.assertIsNone(result.json()['profile']['home_department'])
        self.assertEqual([call[0] for call in self.readers.calls], ['profile'])

    async def test_validation_precedence_is_token_body_shape_before_any_reads(self):
        app = self.app()
        cases = [
            ('preference', {}, {}, b'x'*32769, 401),
            ('profile', {}, {}, b'x'*32769, 401),
            ('preference', None, [('user_id', USER_ID), ('user_id', USER_ID)], b'x'*32769, 413),
            ('profile', None, {}, b'x'*32769, 413),
            ('preference', None, {'user_id': USER_ID}, b' ', 422),
            ('preference', None, [('user_id', USER_ID), ('user_id', USER_ID)], b'', 422),
            ('preference', None, {'user_id': USER_ID, 'extra': 'secret'}, b'', 422),
            ('preference', None, {}, b'', 422),
            ('profile', None, {'extra': 'secret'}, b'{', 422),
            ('profile', None, {}, b'{', 422),
            ('profile', None, {}, b'\xff', 422),
            ('profile', None, {}, b'{"user_id":"syn-cust-01","extra":"private"}', 422),
            ('profile', None, {}, b'{"user_id":12}', 422),
        ]
        for operation, headers, params, content, status in cases:
            with self.subTest(operation=operation, status=status):
                response = await self.get(app, operation=operation, headers=headers, params=params,
                                          content=content)
                self.assertEqual(response.status_code, status)
                self.assertNotIn('secret', response.text)
                self.assertNotIn('private', response.text)
        self.assertEqual(self.readers.calls, [])
        self.assertEqual(self.remaining(), 30)
        self.assert_joined(app)

    async def test_unknown_roster_id_and_unavailable_profile_fail_without_charge(self):
        app = self.app()
        response = await self.get(app, params={'user_id': 'syn-cust-02'})
        self.assert_error(response, 404, 'profile_unavailable')
        self.assertEqual(self.readers.calls, [])
        self.readers.profile_data = deepcopy(EMPTY)
        for operation in ['profile', 'preference']:
            self.assert_error(await self.get(app, operation=operation), 404, 'profile_unavailable')
        self.assertEqual(self.remaining(), 30)

    async def test_corrupt_preference_failure_is_sanitized_and_uncharged(self):
        app = self.app()
        self.readers.preference_data = preference_result('PRIVATE invalid value')
        with self.assertLogs('shopunow.entry', level='INFO') as captured:
            response = await self.get(app)
        self.assert_error(response, 500, 'technical_error')
        self.assertNotIn('PRIVATE', response.text + ' '.join(captured.output))
        self.assertEqual(self.remaining(), 30)
        self.assert_joined(app)

    async def test_exhaustion_does_not_skip_record_validation(self):
        for _ in range(30):
            self.assertTrue(self.allowances.try_commit(Allowance.PROFILE_READ))
        app = self.app()
        self.assert_error(await self.get(app), 429, 'profile_read_limit')
        self.assertEqual([c[0] for c in self.readers.calls], ['profile', 'preference'])
        self.readers.profile_data = deepcopy(EMPTY)
        self.assert_error(await self.get(app), 404, 'profile_unavailable')

    async def test_expiry_during_native_serialization_suppresses_success_but_counts(self):
        app = self.app()
        original = fastapi.routing.serialize_response
        async def late(**kwargs):
            response = await original(**kwargs)
            self.now = 115.0
            return response
        with patch.object(fastapi.routing, 'serialize_response', late):
            response = await self.get(app)
        self.assert_error(response, 503, 'request_timeout')
        self.assertEqual(self.remaining(), 29)
        self.assert_joined(app)

    async def test_serialization_failure_after_charge_has_safe_error(self):
        app = self.app()
        async def broken(**kwargs):
            raise ValueError('PRIVATE native failure')
        with self.assertLogs('shopunow.entry', level='INFO') as captured:
            with patch.object(fastapi.routing, 'serialize_response', broken):
                response = await self.get(app)
        self.assert_error(response, 500, 'technical_error')
        self.assertNotIn('PRIVATE', response.text + ' '.join(captured.output))
        self.assertEqual(self.remaining(), 29)
        self.assert_joined(app)

    async def test_events_for_normal_rejected_failed_paths_are_content_free(self):
        app = self.app()
        with self.assertLogs('shopunow.entry', level='INFO') as captured:
            await self.get(app)
            await self.get(app, headers={})
            self.readers.profile_data = {'private': 'PRIVATE stored data'}
            await self.get(app)
        events = [json.loads(row.split(':', 2)[2]) for row in captured.output]
        allowed = {'utc_time', 'event', 'request_id', 'outcome', 'http_status', 'elapsed_ms'}
        for event in events:
            self.assertLessEqual(set(event), allowed)
            UUID(event['request_id'])
        text = ' '.join(captured.output)
        for secret in [TOKEN, USER_ID, 'PRIVATE', 'response_style', 'Traceback']:
            self.assertNotIn(secret, text)
        self.assertTrue(any('elapsed_ms' in event for event in events))
        self.assertTrue(any(event['event'] == 'identity_completion' for event in events))
        self.assert_joined(app)

    async def test_disconnect_stops_and_joins_http_but_retains_running_capacity(self):
        started, finish = self.block_read()
        app = self.app()
        host, messages, sent, state = await self.start_wire(app)
        await self.thread_event(started)
        messages.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(app)
        await self.assert_running_capacity_retained()
        finish.set()
        result = await state["handle"].lease.wait(2)
        self.assertEqual((result.status, result.charged), ("stopped", False))
        self.assertEqual(self.remaining(), 30)


    async def test_one_direct_host_cancel_joins_without_abandoning_worker(self):
        started, finish = self.block_read()
        app = self.app()
        host, _, sent, state = await self.start_wire(app)
        await self.thread_event(started)
        host.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(app)
        await self.assert_running_capacity_retained()
        finish.set()
        self.assertEqual((await state["handle"].lease.wait(2)).status, "stopped")


    async def test_anyio_scope_cancellation_joins_owned_http_children(self):
        started, finish = self.block_read()
        app = self.app()
        scope, messages, sent, send = self.wire()
        entered, done = anyio.Event(), anyio.Event()
        cancel_scopes = []
        async def server_host():
            try:
                with anyio.CancelScope() as cancel_scope:
                    cancel_scopes.append(cancel_scope)
                    entered.set()
                    await app(scope, messages.get, send)
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
        self.assert_joined(app)
        finish.set()
        await app.states[0]["handle"].lease.wait(2)


    async def test_send_failure_never_sends_preference_or_second_response(self):
        app = self.app()
        calls = []
        async def broken_send(message):
            calls.append(message)
            raise OSError("PRIVATE sender failure")
        scope, messages, _, _ = self.wire()
        with self.assertLogs("shopunow.entry", level="INFO") as captured:
            await app(scope, messages.get, broken_send)
        self.assertEqual([message["type"] for message in calls], ["http.response.start"])
        self.assertTrue(app.states[0]["closed"])
        self.assert_joined(app)
        self.assertEqual(self.remaining(), 29)
        self.assertIn("delivery_failure", " ".join(captured.output))
        self.assertNotIn("PRIVATE", " ".join(captured.output))


    async def test_repeated_direct_cancel_waiting_for_cleanup_thread_token_is_joined(self):
        app = self.app()
        sending = anyio.Event()
        async def blocked_send(message):
            sending.set()
            await anyio.sleep_forever()
        host, _, _, state = await self.start_wire(app, send=blocked_send)
        await sending.wait()
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()
        try:
            host.cancel()
            await state["cleanup_started"].wait()
            host.cancel()
            host.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
        finally:
            limiter.release()
            limiter.total_tokens = original_tokens
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(host, 2)
        self.assert_joined(app)
        self.assertEqual(self.remaining(), 29)


    async def test_control_thread_token_wait_does_not_precede_business_slot(self):
        started, finish = self.block_read()
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()
        app = self.app()
        try:
            host, _, sent, state = await self.start_wire(app)
            self.assertTrue(started.wait(2))  # Business executor is independent of AnyIO tokens.
            self.assertIsNotNone(state["handle"])
            host.cancel()
            host.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
        finally:
            limiter.release()
            limiter.total_tokens = original_tokens
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(app)
        finish.set()
        await state["handle"].lease.wait(2)


    async def test_normal_app_exposes_neither_local_read_route(self):
        app = api.create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            self.assertEqual((await client.get('/preferences')).status_code, 404)
            self.assertEqual((await client.post('/profiles/resolve')).status_code, 404)

    async def test_success_emits_terminal_event_with_measured_elapsed(self):
        app = self.app()
        with self.assertLogs('shopunow.entry', level='INFO') as captured:
            response = await self.get(app, operation='profile')
        terminal = [json.loads(row.split(':', 2)[2]) for row in captured.output
                    if json.loads(row.split(':', 2)[2])['event'] == 'identity_terminal']
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(terminal), 1)
        self.assertEqual((terminal[0]['outcome'], terminal[0]['http_status']), ('ready', 200))
        self.assertGreaterEqual(terminal[0]['elapsed_ms'], 0)

    async def test_withdrawal_during_serialization_suppresses_success_and_counts(self):
        app = self.app()
        original = fastapi.routing.serialize_response
        async def withdrawn(**kwargs):
            response = await original(**kwargs)
            await anyio.to_thread.run_sync(app.states[-1]['handle'].withdraw)
            return response
        with patch.object(fastapi.routing, 'serialize_response', withdrawn):
            response = await self.get(app, operation='profile')
        self.assert_error(response, 503, 'request_stopped')
        self.assertEqual(self.remaining(), 29)
        self.assert_joined(app)

    async def test_full_capacity_rejects_without_waiting_for_thread_token(self):
        finish = threading.Event()
        self.release_events.append(finish)
        leases = [self.slots.submit(self.executor, finish.wait, 3) for _ in range(5)]
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()
        try:
            response = await asyncio.wait_for(self.get(self.app()), 1)
            self.assert_error(response, 503, 'execution_capacity')
            self.assertEqual(self.readers.calls, [])
        finally:
            limiter.release()
            limiter.total_tokens = original_tokens
            finish.set()
        for lease in leases:
            await lease.wait(2)
        self.assertEqual(self.remaining(), 30)

    async def test_identity_mutation_during_serialization_never_sends_rebound_success(self):
        app = self.app()
        original = fastapi.routing.serialize_response
        async def rebound(**kwargs):
            object.__setattr__(kwargs['response_content'], 'user_id', 'syn-cust-02')
            return await original(**kwargs)
        with patch.object(fastapi.routing, 'serialize_response', rebound):
            response = await self.get(app)
        self.assert_error(response, 500, 'technical_error')
        self.assertEqual(self.remaining(), 29)
        self.assert_joined(app)

    async def test_ready_event_cannot_delay_the_final_authorization_before_start(self):
        # A slow handler belongs after response start; it cannot stale pre-send authorization.
        app = self.app()
        scope, messages, sent, send = self.wire()
        observed = []
        original = api.identity_event
        def delayed_event(reference, outcome, status=None, **kwargs):
            if outcome == 'ready' and kwargs.get('stage', 'identity_terminal') == 'identity_terminal':
                observed.append(sum(row['type'] == 'http.response.start' for row in sent))
                self.now = 115.0  # Occurs after authorization/start, never resets the budget.
            return original(reference, outcome, status, **kwargs)
        with patch.object(api, 'identity_event', delayed_event):
            await app(scope, messages.get, send)
        self.assertEqual(observed, [1])
        self.assertEqual([row['status'] for row in sent if row['type'] == 'http.response.start'], [200])
        self.assertEqual(self.remaining(), 29)
        self.assert_joined(app)
