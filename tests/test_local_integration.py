# Python v3.12 / FastAPI v0.143.2 / HTTPX v0.28.1 / limits v5.8.0.
# Exercise shared local components through both existing test-only HTTP applications.
import asyncio  # Own concurrent HTTP requests and actual server-facing ASGI tasks.
import json  # Encode the question's native HTTP request body.
import threading  # Hold supplied storage reads at explicit worker barriers.
import unittest  # Use the repository's existing offline runner.

import anyio  # Wait for thread barriers without blocking the event loop.
import httpx  # Exercise native ASGI requests and FastAPI response serialization.
from limits.storage import MemoryStorage  # Keep the real in-memory rolling allowances.

from shopunow import admission, api  # Compose the actual admission and projection code.
from shopunow.allowances import Allowance, RollingAllowances
from tests.history_http_app import HistoryHTTPApp
from tests.question_http_app import QuestionHTTPApp
from tests.test_admission import ObservedThreadPool
from tests.test_question_admission import SimulatedReaders
from tests.test_records import CONVERSATION_ID, USER_ID, event_result, transcript_pair


TOKEN = "local-shared-token"
SELECTORS = {"user_id": USER_ID, "conversation_id": CONVERSATION_ID}
QUESTION = {**SELECTORS, "question": "Local integration question?"}


class LocalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Specify interactions missing from the individual question/history suites."""

    async def asyncSetUp(self):
        from tests.verified_creation import verified_tracking
        self.tracking = verified_tracking()
        self.slots = admission.ExecutionSlots()  # Both paths share exactly five slots.
        self.reservations = admission.ResourceReservations()  # Questions alone reserve writes.
        self.storage = MemoryStorage()
        self.addCleanup(self.storage.reset)
        self.allowances = RollingAllowances(self.storage, threading.Lock())
        self.executor = ObservedThreadPool(max_workers=12)
        self.addCleanup(self.executor.shutdown, wait=True, cancel_futures=True)
        self.releases = []
        self.tasks = []

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()  # A failing assertion must not strand an owned worker.
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def question_app(self, readers=None):
        return QuestionHTTPApp(
            api_token=TOKEN, slots=self.slots, reservations=self.reservations,
            allowances=self.allowances, executor=self.executor,
            readers=readers or SimulatedReaders(), clock=lambda: 100.0,
            tracking=self.tracking,
        )

    def history_app(self, readers=None):
        return HistoryHTTPApp(
            api_token=TOKEN, slots=self.slots, allowances=self.allowances,
            executor=self.executor, readers=readers or SimulatedReaders(),
            projector=api.project_history_response, clock=lambda: 100.0,
            tracking=self.tracking,
        )

    async def request(self, application, path):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            if path == "/history":
                return await client.get(path, params=SELECTORS, headers={"x-api-token": TOKEN})
            return await client.post(path, json=QUESTION, headers={"x-api-token": TOKEN})

    def start_request(self, application, path):
        task = asyncio.create_task(self.request(application, path))
        self.tasks.append(task)
        return task

    def blocked_readers(self, kind="profile"):
        readers = SimulatedReaders()  # Preserve native flat get-result documents and metadata.
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def block():
            entered.set()  # The barrier proves the actual worker owns its running read.
            if not release.wait(5):
                raise AssertionError("The controlled storage read was not released")

        readers.hooks[kind] = block
        return readers, entered, release

    async def reached(self, event):
        self.assertTrue(await anyio.to_thread.run_sync(event.wait, 2))

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)

    def remaining(self, category, maximum):
        # Native allowance effects independently reveal rejected/stopped extra charges.
        return sum(self.allowances.try_commit(category) for _ in range(maximum + 1))

    async def start_wire(self, application, path):
        # Native ASGI receive is needed to distinguish disconnect from HTTPX completion.
        messages = asyncio.Queue()
        messages.put_nowait({"type": "http.request", "body": json.dumps(QUESTION).encode()
                             if path == "/interactions" else b"", "more_body": False})
        sent = []
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST" if path == "/interactions" else "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"" if path == "/interactions" else
                f"user_id={USER_ID}&conversation_id={CONVERSATION_ID}".encode(),
            "headers": [(b"x-api-token", TOKEN.encode())],
            "client": ("127.0.0.1", 123), "server": ("test", 80),
        }

        async def send(message):
            sent.append(message)

        host = asyncio.create_task(application(scope, messages.get, send))
        self.tasks.append(host)
        await asyncio.wait_for(application.request_started.wait(), 2)
        state = application.states[-1]
        await asyncio.wait_for(state["submitted"].wait(), 2)
        await asyncio.wait_for(state["observer_ready"].wait(), 2)
        return host, messages, sent, state

    async def four_blocked_histories(self):
        tasks, releases = [], []
        for _ in range(4):
            readers, entered, release = self.blocked_readers()
            tasks.append(self.start_request(self.history_app(readers), "/history"))
            releases.append(release)
            await self.reached(entered)
        return tasks, releases

    async def test_five_mixed_http_workers_reject_both_paths_without_extra_allowances(self):
        readers, entered, release = self.blocked_readers()
        question = self.start_request(self.question_app(readers), "/interactions")
        await self.reached(entered)
        histories, releases = await self.four_blocked_histories()
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          503, "execution_capacity")
        self.assert_error(await self.request(self.history_app(), "/history"),
                          503, "execution_capacity")
        release.set()
        for item in releases:
            item.set()
        self.assert_error(await asyncio.wait_for(question, 2), 503, "interaction_not_ready")
        for response in await asyncio.wait_for(asyncio.gather(*histories), 2):
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 9)
        self.assertEqual(self.remaining(Allowance.HISTORY, 30), 26)

    async def test_history_can_read_complete_prefix_while_question_reserves_conversation(self):
        readers, entered, release = self.blocked_readers("event")
        question = self.start_request(self.question_app(readers), "/interactions")
        await self.reached(entered)  # Question reservation precedes its event read.
        history_readers = SimulatedReaders()
        history_readers.event_value = event_result(*transcript_pair())
        response = await self.request(self.history_app(history_readers), "/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([message["text"] for message in response.json()["messages"]],
                         ["  Help with my delivery?  ", "Stored answer 1"])
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          409, "conversation_busy")
        release.set()
        self.assert_error(await asyncio.wait_for(question, 2), 503, "interaction_not_ready")
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          503, "interaction_not_ready")
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 8)
        self.assertEqual(self.remaining(Allowance.HISTORY, 30), 29)

    async def test_near_cap_question_excludes_competitor_without_extra_read_or_charge(self):
        # Twenty-four complete exchanges leave exactly two of fifty event positions free.
        readers, entered, release = self.blocked_readers("event")
        readers.event_value = event_result(*(
            record for number in range(1, 25)
            for record in transcript_pair(number, sequence=2 * number - 1)
        ))
        owner_app = self.question_app(readers)
        owner = self.start_request(owner_app, "/interactions")
        await self.reached(entered)  # The real event read starts only after reservation.
        owner_dispatch = self.executor.dispatched[0]  # Join native callbacks before reuse.

        competitor_readers = SimulatedReaders()
        competitor_readers.event_value = readers.event_value
        competitor = await self.request(self.question_app(competitor_readers), "/interactions")
        self.assert_error(competitor, 409, "conversation_busy")
        self.assertEqual([call[0] for call in competitor_readers.calls], ["profile", "header"])
        self.assertFalse(owner.done())  # The rejected contender cannot finish the held owner.

        release.set()  # Permit the original 48-event read to finish, without storing new events.
        self.assert_error(await asyncio.wait_for(owner, 2), 503, "interaction_not_ready")
        await anyio.to_thread.run_sync(owner_dispatch.result, 2)
        self.assertEqual([call[0] for call in readers.calls], ["profile", "header", "event"])
        self.assertEqual(readers.calls[-1][3], 51)  # Cap plus one must reach the reader boundary.
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 9)  # Only the owner counted.

        # The check above exhausts quota; reaching 429 now proves the key was released.
        fresh_readers = SimulatedReaders()
        fresh_readers.event_value = readers.event_value
        self.assert_error(await self.request(self.question_app(fresh_readers), "/interactions"),
                          429, "question_limit")
        self.assertEqual([call[0] for call in fresh_readers.calls], ["profile", "header", "event"])

    async def test_full_fifty_event_question_rejects_without_consuming_allowance(self):
        # Twenty-five complete exchanges fill the cap; valid history is not corruption.
        readers = SimulatedReaders()
        readers.event_value = event_result(*(
            record for number in range(1, 26)
            for record in transcript_pair(number, sequence=2 * number - 1)
        ))
        response = await self.request(self.question_app(readers), "/interactions")
        self.assert_error(response, 409, "conversation_full")
        self.assertEqual([call[0] for call in readers.calls], ["profile", "header", "event"])
        self.assertEqual(readers.calls[-1][3], 51)  # Full is selected after the bounded read.
        self.assertNotIn("Stored answer", response.text)  # No partial transcript in rejection.
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 10)  # Rejection counts nothing.

    async def test_incomplete_history_neither_charges_nor_releases_running_question_reservation(self):
        readers, entered, release = self.blocked_readers("event")
        question = self.start_request(self.question_app(readers), "/interactions")
        await self.reached(entered)
        history_readers = SimulatedReaders()
        history_readers.event_value = event_result(transcript_pair()[0])
        response = await self.request(self.history_app(history_readers), "/history")
        self.assert_error(response, 409, "conversation_unresolved")
        self.assertNotIn("Help with my delivery", response.text)  # No pending text is history.
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          409, "conversation_busy")
        release.set()
        self.assert_error(await asyncio.wait_for(question, 2), 503, "interaction_not_ready")
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 9)
        self.assertEqual(self.remaining(Allowance.HISTORY, 30), 30)

    async def test_disconnected_question_keeps_capacity_seen_by_history_until_worker_finishes(self):
        readers, entered, release = self.blocked_readers("event")
        app = self.question_app(readers)
        host, messages, sent, state = await self.start_wire(app, "/interactions")
        await self.reached(entered)
        messages.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(host, 2)
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          409, "conversation_busy")
        histories, releases = await self.four_blocked_histories()
        self.assert_error(await self.request(self.history_app(), "/history"),
                          503, "execution_capacity")
        release.set()
        result = await state["handle"].lease.wait(2)
        self.assertEqual((result.status, result.charged), ("stopped", False))
        self.assertEqual(sent, [])  # The late question cannot send through its closed request.
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          503, "interaction_not_ready")  # Its completed worker released the key.
        self.assertEqual((await self.request(self.history_app(), "/history")).status_code, 200)
        for item in releases:
            item.set()
        for response in await asyncio.wait_for(asyncio.gather(*histories), 2):
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 9)
        self.assertEqual(self.remaining(Allowance.HISTORY, 30), 25)

    async def test_disconnected_history_keeps_capacity_seen_by_question_and_cannot_leak_late_data(self):
        readers, entered, release = self.blocked_readers("event")
        readers.event_value = event_result(*transcript_pair())
        app = self.history_app(readers)
        host, messages, sent, state = await self.start_wire(app, "/history")
        await self.reached(entered)
        histories, releases = await self.four_blocked_histories()
        messages.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(host, 2)
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          503, "execution_capacity")
        release.set()
        result = await state["handle"].lease.wait(2)
        self.assertEqual((result.status, result.charged), ("stopped", False))
        self.assertEqual(sent, [])
        fresh_readers = SimulatedReaders()
        fresh_readers.event_value = event_result(*transcript_pair(2))
        response = await self.request(self.history_app(fresh_readers), "/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([message["text"] for message in response.json()["messages"]],
                         ["  Help with my delivery?  ", "Stored answer 2"])
        self.assert_error(await self.request(self.question_app(), "/interactions"),
                          503, "interaction_not_ready")  # A history lease never owns the write key.
        for item in releases:
            item.set()
        for response in await asyncio.wait_for(asyncio.gather(*histories), 2):
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 9)
        self.assertEqual(self.remaining(Allowance.HISTORY, 30), 25)

    async def test_named_http_allowances_exhaust_independently_in_both_directions(self):
        question, history = self.question_app(), self.history_app()
        for _ in range(10):
            self.assert_error(await self.request(question, "/interactions"),
                              503, "interaction_not_ready")
        self.assert_error(await self.request(question, "/interactions"), 429, "question_limit")
        self.assertEqual((await self.request(history, "/history")).status_code, 200)
        self.assertEqual(self.remaining(Allowance.HISTORY, 30), 29)
        self.storage.reset()  # Start a separate empty local window after every request has joined.
        for _ in range(30):
            self.assertEqual((await self.request(history, "/history")).status_code, 200)
        self.assert_error(await self.request(history, "/history"), 429, "history_limit")
        self.assert_error(await self.request(question, "/interactions"),
                          503, "interaction_not_ready")
        self.assertEqual(self.remaining(Allowance.QUESTION, 10), 9)


# Flow: independent HTTP hosts -> shared slots/allowances -> real verifiers/projector
# -> controlled read barriers -> public rejection/success and late-completion isolation.
