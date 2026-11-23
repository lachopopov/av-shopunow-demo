# FastAPI v0.143.2 / HTTPX v0.28.1 / Pydantic v2.14.1
# Real in-process requests exercise validation; no storage or provider is involved.
import json  # Inspect the existing allowlisted operational events.
import unittest  # Use the project's existing offline test runner.
from typing import Annotated  # Declare native FastAPI dependencies.
from uuid import UUID  # Check opaque response/log correlation references.

import httpx  # Drive the real ASGI application without a server or sockets.
from fastapi import Depends, Response  # Keep the validation-only route in this file.

from shopunow import api  # Permit a behavioral RED before the new factory exists.


TOKEN = "history-test-token"  # Synthetic configuration, never a real credential.
QUERY = "user_id=syn-cust-01&conversation_id=" + "a" * 32


def validation_app():
    """Observe validated selectors only; 204 never represents history success."""
    factory = getattr(api, "history_validator", None)
    assert callable(factory), "The history validation dependency is not implemented."
    application = api.create_app()
    application.state.validated = []  # Test-only observation stays outside production.
    validate = factory(api_token=TOKEN)

    @application.get("/history", status_code=204)
    async def validate_only(payload: Annotated[object, Depends(validate)]):
        application.state.validated.append(payload)
        return Response(status_code=204)  # No transcript, admission or charge exists here.

    return application


class HistoryValidationTests(unittest.IsolatedAsyncioTestCase):
    """Catch missing gates, lost multiplicity, unsafe errors and incorrect precedence."""

    async def request(self, query=QUERY, *, body=b"", headers=None, application=None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application or validation_app()),
            base_url="http://test",
        ) as client:
            return await client.request(
                "GET", "/history?" + query, content=body,
                headers={"x-api-token": TOKEN} if headers is None else headers,
            )

    def assert_safe(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(set(response.json()), {"error"})
        error = response.json()["error"]
        self.assertEqual(set(error), {"code", "message", "request_id"})
        self.assertEqual(error["code"], code)
        self.assertEqual(error["message"], {
            401: "The API token is invalid.", 413: "The request body is too large.",
            422: "The request is invalid.", 404: "The operation was not found.",
        }[status])
        UUID(error["request_id"])
        return error["request_id"]

    async def test_valid_query_preserves_native_identifier_syntax(self):
        application = validation_app()
        for conversation in ["a" * 32, "ABCDEF12-3456-7890-ABCD-EF1234567890"]:
            response = await self.request(
                "conversation_id=" + conversation + "&user_id=A_0-1",
                application=application,
            )
            self.assertEqual(response.status_code, 204)
            payload = application.state.validated[-1]
            self.assertEqual(payload.model_dump(), {
                "user_id": "A_0-1", "conversation_id": conversation,
            })

    async def test_missing_blank_malformed_and_unknown_query_values_are_rejected(self):
        for query in [
            "", "user_id=syn-cust-01", "conversation_id=" + "a" * 32,
            QUERY.replace("syn-cust-01", ""), QUERY.replace("syn-cust-01", "../foreign"),
            QUERY.replace("syn-cust-01", "a" * 65), QUERY.replace("a" * 32, "bad"),
            QUERY.replace("a" * 32, ""), QUERY.replace("syn-cust-01", "%FF"),
            QUERY.replace("syn-cust-01", "a%00"), QUERY.replace("syn-cust-01", "a%0A"),
            *[QUERY + "&" + field + "=private" for field in
              ["role", "department", "request_id", "limit", "User_id"]],
        ]:
            with self.subTest(query=query):
                self.assert_safe(await self.request(query), 422, "invalid_request")

    async def test_duplicate_decoded_keys_reject_even_identical_values(self):
        for suffix in [
            "&user_id=syn-cust-01", "&user_id=other", "&user%5Fid=syn-cust-01",
            "&conversation_id=" + "a" * 32, "&conversation_id=bad",
        ]:
            with self.subTest(suffix=suffix):
                self.assert_safe(await self.request(QUERY + suffix), 422, "invalid_request")

    async def test_bad_tokens_precede_body_and_query_rejection(self):
        for headers in [{}, {"x-api-token": "wrong"},
                        [("x-api-token", TOKEN), ("x-api-token", TOKEN)]]:
            with self.subTest(headers=headers):
                response = await self.request("role=private", body=b"x" * 32769,
                                              headers=headers)
                self.assert_safe(response, 401, "unauthorized")

    async def test_nonempty_body_rejects_without_json_or_encoding_requirements(self):
        for body in [b" ", b"{}", b"\xff", b"x" * 32768]:
            with self.subTest(length=len(body)):
                self.assert_safe(await self.request(body=body), 422, "invalid_request")

    async def test_oversize_precedes_nonempty_body_and_invalid_query(self):
        for query in [QUERY, "", QUERY + "&role=private"]:
            self.assert_safe(await self.request(query, body=b"x" * 32769),
                             413, "body_too_large")

    async def asgi_request(self, *, headers, chunks, query=QUERY, forbid_receive=False):
        # Native ASGI frames allow absent/false lengths and split-body boundary checks.
        frames = iter([
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            for index, chunk in enumerate(chunks)
        ])
        sent = []

        async def receive():
            if forbid_receive:
                self.fail("Invalid tokens must be rejected before receiving body bytes.")
            return next(frames)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/history",
            "raw_path": b"/history", "query_string": query.encode(), "headers": headers,
            "client": ("127.0.0.1", 123), "server": ("test", 80),
        }
        await validation_app()(scope, receive, send)
        return sent[0]["status"]

    async def test_token_failure_never_consumes_body_even_for_nonascii_or_duplicates(self):
        for headers in [[], [(b"x-api-token", b"\xff")],
                        [(b"x-api-token", TOKEN.encode())] * 2]:
            self.assertEqual(await self.asgi_request(
                headers=headers, chunks=[], query="bad=private", forbid_receive=True,
            ), 401)

    async def test_actual_split_stream_bytes_override_missing_or_false_content_length(self):
        for length in [None, b"0", b"1", b"999999"]:
            headers = [(b"x-api-token", TOKEN.encode())]
            if length is not None:
                headers.append((b"content-length", length))
            for chunks, status in [([b"", b""], 204), ([b"x" * 32768], 422),
                                   ([b"x", b"x" * 32767, b"x"], 413)]:
                with self.subTest(length=length, expected=status):
                    self.assertEqual(await self.asgi_request(headers=headers, chunks=chunks), status)

    async def test_validation_and_rejection_logs_exclude_token_body_and_selectors(self):
        with self.assertLogs("shopunow.entry", level="INFO") as capture:
            accepted = await self.request()
            rejected = await self.request(QUERY + "&role=private-value", body=b"secret-body")
            invalid_query = await self.request(QUERY + "&role=private-value")
        self.assertEqual(accepted.status_code, 204)
        references = [self.assert_safe(item, 422, "invalid_request")
                      for item in [rejected, invalid_query]]
        events = [json.loads(record.getMessage()) for record in capture.records]
        self.assertEqual([event["outcome"] for event in events],
                         ["validated", "invalid_request", "invalid_request"])
        self.assertEqual([event["request_id"] for event in events[1:]], references)
        for event in events:
            self.assertLessEqual(set(event),
                                 {"utc_time", "event", "request_id", "outcome", "http_status"})
            UUID(event["request_id"])
        output = " ".join(capture.output) + rejected.text + invalid_query.text
        for private in [TOKEN, "syn-cust-01", "a" * 32, "secret-body", "private-value", "Traceback"]:
            self.assertNotIn(private, output)

    async def test_normally_created_app_has_no_served_history_route(self):
        self.assert_safe(await self.request(application=api.create_app()), 404, "not_found")

    async def test_configuration_requires_an_explicit_nonempty_ascii_token(self):
        factory = getattr(api, "history_validator", None)
        self.assertTrue(callable(factory), "The history validation factory is missing.")
        for token in ["", "é"]:
            with self.subTest(token=token), self.assertRaises(ValueError):
                factory(api_token=token)
        with self.assertRaises(TypeError):
            factory()  # No implicit environment or credential loading is permitted.


# Overall flow: test-only native dependency route -> wire inputs -> safe validation outcome.
