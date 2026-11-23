# Python v3.12 / FastAPI v0.143.2 / limits v5.8.0; simulated local boundaries only.
import asyncio  # Distinguish server-facing raw cancellation from native AnyIO scopes.
import json  # Inspect safe wire envelopes and allowlisted events.
import threading  # Supply the real allowance guard and controlled read barriers.
import unittest  # Keep the repository's native test runner.
from unittest.mock import patch  # Control only time, native serialization and boundary faults.
from uuid import UUID  # Validate opaque generated references.
import anyio  # Exercise owned task groups and native thread-token acquisition.
import httpx  # Drive the actual in-process FastAPI application.
import fastapi.routing  # Wrap the actual installed native response serializer.
from limits.storage import MemoryStorage  # Exercise real allowance effects.
from starlette.requests import ClientDisconnect  # Preserve native incomplete-body behavior.
from shopunow import admission, api  # Exercise the real coordinator and projector.
from shopunow.allowances import Allowance, RollingAllowances
from tests.test_admission import InlineExecutor, ObservedLock, ObservedThreadPool, SubmitThenRaise
from tests.test_question_admission import SimulatedReaders
from tests.test_records import (USER_ID, CONVERSATION_ID, HEADER, event_result, header_result,
                               transcript_pair, transcript_marker, transcript_offer)

TOKEN = "local-history-token"
VALID = {"user_id": USER_ID, "conversation_id": CONVERSATION_ID}


class HistoryHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.slots = admission.ExecutionSlots()
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
            event.set()  # Assertion failures must release every controlled worker.
        for task in self.hosts:
            if not task.done():
                task.cancel()
        if self.hosts:
            await asyncio.gather(*self.hosts, return_exceptions=True)

    def app(self, **options):
        self.assertTrue(callable(getattr(admission, "submit_history_admission_async", None)),
                        "History HTTP needs an immediate async-safe submission boundary")
        from tests.history_http_app import HistoryHTTPApp
        return HistoryHTTPApp(api_token=TOKEN, slots=self.slots, allowances=self.allowances,
                              executor=options.pop("executor", self.executor),
                              readers=options.pop("readers", self.readers),
                              projector=options.pop("projector", api.project_history_response),
                              clock=options.pop("clock", lambda: self.now), **options)

    async def get(self, app, *, params=VALID, headers=None, content=b""):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,
                                                                  raise_app_exceptions=False),
                                     base_url="http://test") as client:
            return await client.request("GET", "/history", params=params, content=content,
                                        headers={"x-api-token": TOKEN} if headers is None else headers)

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        payload = response.json()
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"code", "message", "request_id"})
        self.assertEqual(payload["error"]["code"], code)
        UUID(payload["error"]["request_id"])
        self.assertNotIn("charged", response.text)
        self.assertNotIn("retry-after", response.headers)
        return payload["error"]

    def assert_joined(self, application):
        for state in application.states:
            self.assertTrue(state["application_done"])
            if state["observer_started"]:
                self.assertTrue(state["observer_done"])

    def remaining(self):
        return sum(self.allowances.try_commit(Allowance.HISTORY) for _ in range(31))

    async def thread_event(self, event):
        self.assertTrue(await anyio.to_thread.run_sync(event.wait, 2))

    def block_read(self):
        started, finish = threading.Event(), threading.Event()
        self.release_events.append(finish)
        def blocked():
            started.set()
            if not finish.wait(3):
                raise AssertionError("The controlled transcript read was not released")
        self.readers.hooks["event"] = blocked
        return started, finish

    def wire(self):
        messages = asyncio.Queue()
        messages.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        sent = []
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/history",
                 "raw_path": b"/history", "query_string": (
                     f"user_id={USER_ID}&conversation_id={CONVERSATION_ID}".encode()),
                 "headers": [(b"x-api-token", TOKEN.encode())],
                 "client": ("127.0.0.1", 123), "server": ("test", 80)}
        async def send(message):
            sent.append(message)
        return scope, messages, sent, send

    async def start_wire(self, application, *, send=None):
        scope, messages, sent, default_send = self.wire()
        task = asyncio.create_task(application(scope, messages.get, send or default_send))
        self.hosts.append(task)
        await application.request_started.wait()
        state = application.states[-1]
        await state["submitted"].wait()
        await state["observer_ready"].wait()
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

    async def test_owned_empty_history_uses_native_success_and_charges_once(self):
        # Missing wiring, response binding, or the final HISTORY hit breaks this behavior.
        application = self.app()
        response = await self.get(application)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"request_id", "user_id", "conversation_id", "messages", "notices"})
        self.assertEqual(payload["user_id"], USER_ID)
        self.assertEqual(payload["conversation_id"], CONVERSATION_ID)
        self.assertEqual((payload["messages"], payload["notices"]), ([], []))
        self.assertEqual(sum(self.allowances.try_commit(Allowance.HISTORY) for _ in range(31)), 29)
        self.assert_joined(application)

    async def test_complete_messages_and_notices_preserve_public_shape(self):
        # Returning internal records or losing technical offer_id:null breaks this public contract.
        self.readers.event_value = event_result(*transcript_pair(with_offer=True),
                                              *transcript_pair(2, 3, kind="failed_technical"))
        self.readers.offers[str(UUID(int=1001))] = transcript_offer(1)
        application = self.app()
        response = await self.get(application)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][1]["text"], "Stored answer 1")
        self.assertEqual(set(payload["messages"][0]),
                         {"message_id", "request_id", "sequence", "speaker", "text"})
        self.assertEqual(len(payload["notices"]), 2)
        self.assertEqual(payload["notices"][1]["reason_code"], "technical_error")
        self.assertIsNone(payload["notices"][1]["offer_id"])
        self.assertEqual(self.remaining(), 29)
        self.assertTrue(all(self.allowances.try_commit(Allowance.QUESTION) for _ in range(10)))
        self.assertTrue(all(self.allowances.try_commit(Allowance.PROFILE_READ) for _ in range(30)))
        self.assert_joined(application)

    async def test_full_valid_history_readable_but_overflow_and_partial_history_never_charge(self):
        for count, status in [(50, 200), (51, 409)]:
            readers = SimulatedReaders()
            readers.event_value = event_result(*(transcript_marker(i) for i in range(1, count + 1)))
            readers.offers = {str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain")
                              for i in range(1, count + 1)}
            response = await self.get(self.app(readers=readers))
            self.assertEqual(response.status_code, status)
            if count == 50:
                self.assertEqual(len(response.json()["notices"]), 50)
            else:
                self.assert_error(response, 409, "conversation_unresolved")
        self.readers.event_value = event_result(transcript_pair()[0])
        self.assert_error(await self.get(self.app()), 409, "conversation_unresolved")
        self.assertEqual(self.remaining(), 29)

    async def test_validation_precedence_before_capacity_and_reads(self):
        finish = threading.Event()
        self.release_events.append(finish)
        leases = [self.slots.submit(self.executor, finish.wait, 3) for _ in range(5)]
        application = self.app()
        for params, content, headers, status, code in [
            ({}, b"x" * 32769, {}, 401, "unauthorized"),
            ({}, b"x" * 32769, {"x-api-token": TOKEN}, 413, "body_too_large"),
            (VALID, b" ", {"x-api-token": TOKEN}, 422, "invalid_request"),
            ({**VALID, "extra": "private"}, b"", {"x-api-token": TOKEN}, 422, "invalid_request"),
            (VALID, b"", {"x-api-token": TOKEN}, 503, "execution_capacity"),
        ]:
            self.assert_error(await self.get(application, params=params, content=content,
                                             headers=headers), status, code)
        self.assertEqual(self.readers.calls, [])
        self.assertEqual(self.remaining(), 30)
        self.assert_joined(application)
        finish.set()
        for lease in leases:
            await lease.wait(2)

    async def test_absent_and_foreign_ownership_errors_are_equivalent(self):
        foreign = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
        foreign["metadatas"][0]["owner_user_id"] = "syn-cust-02"
        errors = []
        for raw in [event_result(), foreign]:
            readers = SimulatedReaders()
            readers.header_value = raw
            app = self.app(readers=readers)
            error = self.assert_error(await self.get(app), 404, "ownership_failure")
            errors.append({key: value for key, value in error.items() if key != "request_id"})
            self.assertEqual([call[0] for call in readers.calls], ["profile", "header"])
            self.assert_joined(app)
        self.assertEqual(errors[0], errors[1])
        self.assertEqual(self.remaining(), 30)

    async def test_history_limit_only_after_valid_history_and_safe_technical_mapping(self):
        for _ in range(30):
            self.assertTrue(self.allowances.try_commit(Allowance.HISTORY))
        self.readers.event_value = event_result(transcript_pair()[0])
        self.assert_error(await self.get(self.app()), 409, "conversation_unresolved")
        self.readers.event_value = event_result()
        error = self.assert_error(await self.get(self.app()), 429, "history_limit")
        self.assertEqual(error["message"], "The history allowance is exhausted.")
        def failed():
            raise RuntimeError("PRIVATE storage error")
        self.readers.hooks["profile"] = failed
        self.assert_error(await self.get(self.app()), 500, "technical_error")

    async def test_late_true_false_and_exception_hits_all_return_timeout(self):
        native_hit = self.allowances._try_commit_locked
        for outcome, charged in [(True, True), (False, False), (None, None)]:
            self.now = 100.0
            def late(category):
                if outcome is True:
                    native_hit(category)
                self.now = 115.0
                if outcome is None:
                    raise RuntimeError("PRIVATE unknown hit")
                return outcome
            app = self.app()
            with patch.object(self.allowances, "_try_commit_locked", side_effect=late):
                self.assert_error(await self.get(app), 503, "request_timeout")
            self.assertIs(app.states[0]["handle"].attempt.charged, charged)
            self.assert_joined(app)
        self.assertEqual(self.remaining(), 29)

    async def test_expiry_after_actual_native_serialization_replaces_entire_success(self):
        # Without the last ASGI-start check, serialized private history escapes after expiry.
        self.readers.event_value = event_result(*transcript_pair())
        native_serialize = fastapi.routing.serialize_response
        serialized = []
        async def serialize_then_expire(**kwargs):
            result = await native_serialize(**kwargs)  # Exercise native validation and JSON bytes.
            serialized.append(result)
            self.now = 115.0
            return result
        app = self.app()
        with patch.object(fastapi.routing, "serialize_response", serialize_then_expire):
            response = await self.get(app)
        self.assertTrue(serialized)
        error = self.assert_error(response, 503, "request_timeout")
        self.assertEqual(error["message"], "The request timed out.")
        self.assertNotIn("Stored answer", response.text)
        self.assertEqual(int(response.headers["content-length"]), len(response.content))
        self.assertTrue(app.states[0]["success_suppressed"])
        self.assertEqual(self.remaining(), 29)
        self.assert_joined(app)

    async def test_outbound_guard_wait_rechecks_deadline_after_native_serialization(self):
        native_serialize = fastapi.routing.serialize_response
        native_state = admission.HistoryAttempt.checked_state
        serialized, reached = anyio.Event(), anyio.Event()
        finish = threading.Event()
        self.release_events.append(finish)
        loop = asyncio.get_running_loop()
        async def observe_serialization(**kwargs):
            result = await native_serialize(**kwargs)
            serialized.set()
            return result
        def delayed_state(attempt, result=None):
            if serialized.is_set():
                loop.call_soon_threadsafe(reached.set)
                if not finish.wait(3):
                    raise AssertionError("Outbound control was not released")
            return native_state(attempt, result)
        app = self.app()
        with patch.object(fastapi.routing, "serialize_response", observe_serialization), \
             patch.object(admission.HistoryAttempt, "checked_state", delayed_state):
            task = asyncio.create_task(self.get(app))
            self.hosts.append(task)
            await reached.wait()
            self.assertFalse(task.done())
            self.now = 115.0
            finish.set()
            self.assert_error(await asyncio.wait_for(task, 2), 503, "request_timeout")
        self.assertEqual(self.remaining(), 29)
        self.assert_joined(app)

    async def test_normally_served_factory_has_no_history(self):
        self.assert_error(await self.get(api.create_app()), 404, "not_found")

    async def test_native_serialization_failure_retains_charge_and_rechecks_timeout(self):
        # A native serialization exception must select current timeout before generic failure.
        native_serialize = fastapi.routing.serialize_response
        for late, status, code in [(False, 500, "technical_error"), (True, 503, "request_timeout")]:
            self.now = 100.0
            async def fail_after_native_serialization(**kwargs):
                await native_serialize(**kwargs)
                if late:
                    self.now = 115.0
                raise RuntimeError("PRIVATE serializer payload")
            app = self.app()
            with patch.object(fastapi.routing, "serialize_response", fail_after_native_serialization):
                self.assert_error(await self.get(app), status, code)
            self.assertTrue(app.states[0]["handle"].attempt.charged)
            self.assert_joined(app)
        self.assertEqual(self.remaining(), 28)

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

    async def test_send_failure_never_sends_history_or_second_response(self):
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

    async def test_observer_remains_through_send_and_disconnect_suppresses_history_body(self):
        app = self.app()
        sending = anyio.Event()
        sent = []
        async def blocked_send(message):
            sent.append(message)
            sending.set()
            await anyio.sleep_forever()  # This is an intentionally blocked outbound boundary.
        host, messages, _, state = await self.start_wire(app, send=blocked_send)
        await sending.wait()
        self.assertFalse(state["observer_done"])
        messages.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(host, 2)
        self.assertEqual([message["type"] for message in sent], ["http.response.start"])
        self.assert_joined(app)
        self.assertEqual(self.remaining(), 29)

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

    async def test_full_business_capacity_rejects_even_with_thread_tokens_exhausted(self):
        finish = threading.Event()
        self.release_events.append(finish)
        leases = [self.slots.submit(self.executor, finish.wait, 3) for _ in range(5)]
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire()
        milestone = anyio.Event()
        native_control = anyio.to_thread.run_sync
        async def observe_control(*args, **kwargs):
            milestone.set()  # Native control then really waits for the occupied thread token.
            return await native_control(*args, **kwargs)
        app = self.app()
        try:
            with patch.object(anyio.to_thread, "run_sync", observe_control):
                host = asyncio.create_task(self.get(app))
                self.hosts.append(host)
                host.add_done_callback(lambda task: milestone.set())
                await milestone.wait()  # Either response finished or blocked control really entered.
                completed_before_token_release = host.done()
        finally:
            limiter.release()
            limiter.total_tokens = original_tokens
            finish.set()
        self.assert_error(await asyncio.wait_for(host, 2), 503, "execution_capacity")
        self.assertTrue(completed_before_token_release,
                        "A full business slot must reject without waiting for a control thread token")
        self.assertEqual(self.readers.calls, [])
        self.assert_joined(app)
        for lease in leases:
            await lease.wait(2)

    async def test_attempt_guard_wait_keeps_event_loop_and_repeated_cancellation_owned(self):
        entered, control_wait = anyio.Event(), anyio.Event()
        finish = threading.Event()
        self.release_events.append(finish)
        loop = asyncio.get_running_loop()
        native_hit = self.allowances._try_commit_locked
        def blocked_hit(category):
            loop.call_soon_threadsafe(entered.set)
            if not finish.wait(3):
                raise AssertionError("Allowance hit was not released")
            return native_hit(category)
        app = self.app()
        with patch.object(self.allowances, "_try_commit_locked", side_effect=blocked_hit):
            host, _, sent, state = await self.start_wire(app)
            await entered.wait()
            handle = state["handle"]
            handle.attempt._lock = ObservedLock(
                handle.attempt._lock, lambda: loop.call_soon_threadsafe(control_wait.set))
            # A separate real withdrawal must enter the held attempt guard off the loop.
            stopping = asyncio.create_task(api.stop_history_admission(handle))
            self.hosts.append(stopping)
            await control_wait.wait()
            progressed = anyio.Event()
            loop.call_soon(progressed.set)
            await progressed.wait()
            host.cancel()
            host.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
            finish.set()
            await stopping
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(host, 2)
        self.assertEqual(sent, [])
        self.assert_joined(app)
        self.assertTrue(handle.attempt.charged)

    async def test_dispatch_after_start_failure_tracks_worker_without_returned_handle(self):
        entered, finish = threading.Event(), threading.Event()
        self.release_events.append(finish)
        native_hit = self.allowances._try_commit_locked
        def blocked_hit(category):
            entered.set()
            if not finish.wait(3):
                raise AssertionError("Dispatch-failure hit was not released")
            return native_hit(category)
        app = self.app(executor=SubmitThenRaise(self.executor, started=entered))
        scope, messages, sent, send = self.wire()
        with self.assertLogs("shopunow.entry", level="INFO") as captured, \
             patch.object(self.allowances, "_try_commit_locked", side_effect=blocked_hit):
            host = asyncio.create_task(app(scope, messages.get, send))
            self.hosts.append(host)
            await app.request_started.wait()
            await self.thread_event(entered)
            self.assertIsNone(app.states[0]["handle"])
            await self.assert_running_capacity_retained()
            host.cancel()
            host.cancel()
            await anyio.lowlevel.checkpoint()
            self.assertFalse(host.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(host, 2)
            await anyio.to_thread.run_sync(self.executor.dispatched[0].result, 2)
        self.assertEqual(sent, [])
        self.assert_joined(app)
        self.assertIn("history_worker_finished", " ".join(captured.output))
        self.assertEqual(self.remaining(), 29)

    async def test_incomplete_body_disconnect_never_dispatches_or_starts_second_reader(self):
        app = self.app()
        scope, messages, sent, send = self.wire()
        messages.get_nowait()
        messages.put_nowait({"type": "http.request", "body": b"", "more_body": True})
        messages.put_nowait({"type": "http.disconnect"})
        with self.assertRaises(ClientDisconnect):
            await app(scope, messages.get, send)
        self.assertEqual(sent, [])
        self.assertEqual(self.readers.calls, [])
        self.assertFalse(app.states[0]["observer_started"])
        self.assert_joined(app)
        self.assertEqual(self.remaining(), 30)

    async def test_normal_rejected_and_failed_events_are_content_free_and_correlated(self):
        app = self.app()
        def failed():
            raise RuntimeError("PRIVATE contact@example.test")
        with self.assertLogs("shopunow.entry", level="INFO") as captured:
            normal = await self.get(app)
            rejected = await self.get(app, headers={"x-api-token": "PRIVATE wrong token"})
            self.readers.hooks["profile"] = failed
            failed_response = await self.get(app)
        references = {normal.json()["request_id"], rejected.json()["error"]["request_id"],
                      failed_response.json()["error"]["request_id"]}
        self.assertEqual(len(references), 3)
        events = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual({event["request_id"] for event in events}, references)
        for event in events:
            self.assertLessEqual(set(event), {"utc_time", "event", "request_id", "outcome", "http_status"})
            UUID(event["request_id"])
        text = " ".join(captured.output) + rejected.text + failed_response.text
        for sentinel in [TOKEN, "PRIVATE", USER_ID, CONVERSATION_ID, "Traceback", "charged"]:
            self.assertNotIn(sentinel, text)
        self.assertIn("history_completion", " ".join(captured.output))
        self.assertIn("history_admission", " ".join(captured.output))
        self.assert_joined(app)


# Flow: native HTTP -> real validation/coordinator/projector -> outward JSON and quota effects.
