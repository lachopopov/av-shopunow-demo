# HTTPX v0.28.1; Python unittest exercises the real in-process ASGI application.
import json  # Encode literal test inputs without using application validators.
import unittest  # Run offline behavior checks without another test dependency.
from typing import Annotated  # Declare FastAPI dependency injection explicitly.
from uuid import UUID  # Check that returned request references are opaque UUIDs.

import httpx  # Send requests directly to the application, without a network server.
from fastapi import Depends, Response, HTTPException  # Test routes remain local.

from shopunow.api import create_app, question_validator  # Exercise application logic.


TOKEN = "local-test-token"  # Synthetic test configuration, never an account credential.
VALID = {"user_id": "syn-cust-01", "conversation_id": "a" * 32, "question": "Help?"}


def validation_app(*, body_limit: int = 32768):
    """Keep the non-serving endpoint in test utilities, outside production code."""
    application = create_app()  # The normal factory registers no interaction endpoint.
    application.state.validated = []  # Test-only observation of unchanged validated text.
    validate = question_validator(api_token=TOKEN, body_limit=body_limit)

    @application.post("/interactions", status_code=204)
    async def validate_only(payload: Annotated[object, Depends(validate)]):
        # 204 means this test harness reached validation's end, not workflow success.
        application.state.validated.append(payload)
        return Response(status_code=204)

    return application


class EntryValidationTests(unittest.IsolatedAsyncioTestCase):
    """Mutations removing gates or swapping their order must fail these checks."""

    async def asyncSetUp(self):
        # ASGITransport executes real FastAPI code without sockets or providers.
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=validation_app()), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()  # Close only the locally owned test client.

    def assert_safe(self, response, status, code):
        # Check the public envelope; never derive expected output from implementation.
        self.assertEqual(response.status_code, status)
        error = response.json()["error"]
        self.assertEqual(set(error), {"code", "message", "request_id"})
        self.assertEqual(error["code"], code)
        self.assertTrue(error["message"])
        UUID(error["request_id"])  # A random reference must not echo input or selectors.
        return error

    async def test_token_rejection_precedes_oversize_and_invalid_json(self):
        response = await self.client.post("/interactions", content=b"{" * 32769)
        self.assert_safe(response, 401, "unauthorized")

    async def test_wrong_token_rejects_valid_question(self):
        response = await self.client.post(
            "/interactions", json=VALID, headers={"X-API-Token": "wrong"}
        )
        self.assert_safe(response, 401, "unauthorized")

    async def test_body_limit_precedes_json_parsing(self):
        response = await self.client.post(
            "/interactions", content=b"{" * 32769, headers={"X-API-Token": TOKEN}
        )
        self.assert_safe(response, 413, "body_too_large")

    async def test_invalid_shapes_and_text_are_safe_and_not_echoed(self):
        # Each literal catches a distinct missing field/type/text/control boundary.
        invalid = [
            {}, {**VALID, "question": None}, {**VALID, "question": 1},
            {**VALID, "question": ""}, {**VALID, "question": " \t\n"},
            {**VALID, "question": "x" * 2001}, {**VALID, "question": "\ud800"},
            {**VALID, "user_id": "../foreign"}, {**VALID, "user_id": ""},
            {**VALID, "conversation_id": "not-a-conversation-id"},
            {**VALID, "role": "employee"}, [], None,
        ]
        for payload in invalid:
            with self.subTest(payload=repr(payload)[:100]):
                response = await self.client.post(
                    "/interactions", content=json.dumps(payload).encode(),
                    headers={"X-API-Token": TOKEN},
                )
                error = self.assert_safe(response, 422, "invalid_request")
                self.assertEqual(error["message"], "The request is invalid.")

    async def test_malformed_json_and_wire_encoding_are_safe(self):
        for body in [b"{", b"", b'"text"', b'{"question":"\xff"}', b"[" * 1000]:
            with self.subTest(body=body[:20]):
                response = await self.client.post(
                    "/interactions", content=body, headers={"X-API-Token": TOKEN}
                )
                self.assert_safe(response, 422, "invalid_request")

    async def test_valid_boundary_encodings_reach_validation_end(self):
        # 2,000 astral characters become 24,000 bytes when escaped in JSON.
        for text in ["x" * 2000, "😀" * 2000, "\"\\\n" * 666, "  Help?  "]:
            for escaped in [True, False]:
                with self.subTest(characters=len(text), escaped=escaped):
                    body = json.dumps({**VALID, "question": text, "response_style":"detailed"},
                                      ensure_ascii=escaped).encode()
                    response = await self.client.post(
                        "/interactions", content=body, headers={"X-API-Token": TOKEN}
                    )
                    self.assertEqual(response.status_code, 204)

    async def test_normal_app_has_no_test_interaction_endpoint(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            response = await client.post("/interactions", json=VALID)
        self.assert_safe(response, 404, "not_found")

    async def test_text_preserved_and_query_controls_rejected(self):
        application = validation_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                "/interactions", json={**VALID, "question": "  Help?  "},
                headers={"X-API-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 204)
            self.assertEqual(application.state.validated[0].question, "  Help?  ")
            response = await client.post(
                "/interactions?role=employee", json=VALID, headers={"X-API-Token": TOKEN}
            )
            self.assert_safe(response, 422, "invalid_request")
            self.assertEqual(len(application.state.validated), 1)

    async def test_actual_stream_bytes_determine_limit(self):
        # Without actual-byte counting, absent/understated lengths bypass protection.
        body = json.dumps(VALID).encode()
        for length in [None, b"1", str(len(body)).encode()]:
            for limit, expected in [(len(body), 204), (len(body) - 1, 413)]:
                messages = iter([
                    {"type": "http.request", "body": body[:20], "more_body": True},
                    {"type": "http.request", "body": body[20:], "more_body": False},
                ])
                sent = []

                async def receive():
                    return next(messages)  # Real ASGI receive frames, not a mocked validator.

                async def send(message):
                    sent.append(message)

                headers = [(b"x-api-token", TOKEN.encode())]
                if length is not None:
                    headers.append((b"content-length", length))
                scope = {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "POST", "scheme": "http", "path": "/interactions",
                    "raw_path": b"/interactions", "query_string": b"", "headers": headers,
                    "client": ("127.0.0.1", 123), "server": ("test", 80),
                }
                with self.subTest(length=length, limit=limit):
                    await validation_app(body_limit=limit)(scope, receive, send)
                    self.assertEqual(sent[0]["status"], expected)

    async def test_token_rejection_does_not_read_stream(self):
        async def receive():
            self.fail("An unauthenticated request must not consume body bytes.")

        sent = []

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/interactions",
            "raw_path": b"/interactions", "query_string": b"",
            "headers": [(b"x-api-token", b"\xff")],
            "client": ("127.0.0.1", 123), "server": ("test", 80),
        }
        await validation_app()(scope, receive, send)
        self.assertEqual(sent[0]["status"], 401)

    async def test_normal_rejected_failed_logs_are_content_free_and_correlated(self):
        application = validation_app()

        @application.get("/controlled-failure")
        async def controlled_failure():
            raise HTTPException(500, detail="private-contact@example.test")

        with self.assertLogs("shopunow.entry", level="INFO") as capture:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                await client.post("/interactions", json=VALID, headers={"X-API-Token": TOKEN})
                rejected = await client.post("/interactions", json=VALID)
                failed = await client.get("/controlled-failure")
        rejected_id = self.assert_safe(rejected, 401, "unauthorized")["request_id"]
        failed_id = self.assert_safe(failed, 500, "technical_error")["request_id"]
        try:
            events = [json.loads(record.getMessage()) for record in capture.records]
        except json.JSONDecodeError:
            self.fail("Operational events must be structured JSON records.")
        self.assertEqual(len(events), 3)
        self.assertEqual([e["outcome"] for e in events], ["validated", "unauthorized", "technical_error"])
        self.assertEqual(events[1]["request_id"], rejected_id)
        self.assertEqual(events[2]["request_id"], failed_id)
        for event in events:
            self.assertLessEqual(set(event), {"utc_time", "event", "request_id", "outcome", "http_status"})
            self.assertIn("utc_time", event)
            UUID(event["request_id"])
        text = " ".join(capture.output) + failed.text
        for forbidden in [TOKEN, VALID["user_id"], VALID["conversation_id"], VALID["question"], "private-contact", "Traceback"]:
            self.assertNotIn(forbidden, text)

    async def test_unexpected_failure_uses_safe_envelope_and_event(self):
        # Removing the native catch-all handler must expose the default unsafe shape.
        application = create_app()

        @application.get("/unexpected-failure")
        async def unexpected_failure():
            raise RuntimeError("private-contact@example.test")

        # A server returns the response even though Starlette re-raises to its caller.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/unexpected-failure")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["content-type"], "application/json")
        _error = self.assert_safe(response, 500, "technical_error")
        with self.assertLogs("shopunow.entry", level="INFO") as capture:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.get("/unexpected-failure")
        event = json.loads(capture.records[0].getMessage())
        self.assertEqual(event["request_id"], response.json()["error"]["request_id"])
        self.assertEqual(event["outcome"], "technical_error")
        self.assertNotIn("private-contact", response.text + " ".join(capture.output))


# Overall flow: build a test-only route, send boundary inputs, inspect safe outcomes.
