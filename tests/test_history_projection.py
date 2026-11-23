# Python 3.12 / Pydantic v2.14.1
# Exercise verified native fixtures and actual outward JSON, without storage or HTTP.
import json  # Inspect the bytes-facing native serialization contract.
import unittest  # Run bounded offline behavior checks.

from shopunow import api, schemas  # Discover missing features through assertions.
from shopunow.records import verify_complete_transcript  # Keep real verification in setup.
from tests.test_records import (  # Reuse complete native fixtures, not a parallel format.
    CONVERSATION_ID, USER_ID, event_result, header_result, transcript_pair,
)

READ_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"  # A fresh history-read reference.


class HistoryProjectionTests(unittest.TestCase):
    """Catch outward-field leaks and changes to historical identity/text."""

    def verified(self, *rows, offers=None):
        checked = verify_complete_transcript(
            header_result(), event_result(*rows), user_id=USER_ID,
            conversation_id=CONVERSATION_ID, offer_results=offers or {},
        )  # Structural fixture validation is real, not mocked.
        self.assertEqual(checked.status, "verified")
        return checked.record

    def project(self, transcript, **changes):
        projector = getattr(api, "project_history_response", None)
        self.assertTrue(callable(projector), "Pure history projection is missing")
        selectors = schemas.HistorySelectors(
            **{"request_id": READ_ID, "user_id": USER_ID,
               "conversation_id": CONVERSATION_ID, **changes},
        )
        return projector(transcript, selectors=selectors)

    def test_empty_history_has_exact_public_envelope(self):
        # An internal transcript dump would leak capacity/counts instead of this envelope.
        response = self.project(self.verified())
        self.assertEqual(json.loads(response.model_dump_json()), {
            "request_id": READ_ID, "user_id": "syn-cust-01",
            "conversation_id": "01234567-89ab-4cde-8fab-0123456789ab",
            "messages": [], "notices": [],
        })

    def test_completed_pair_preserves_stored_identity_text_and_kind(self):
        # Renumbering, trimming or substituting the read ID breaks the literal wire contract.
        pending, terminal = transcript_pair()
        terminal["metadatas"][0]["cleared_citation_ids"] = ["private-evidence"]
        response = self.project(self.verified(terminal, pending))
        self.assertEqual(json.loads(response.model_dump_json())["messages"], [
            {"message_id": "pending_message:00000000000000000000000000000001",
             "request_id": "00000000-0000-0000-0000-000000000001", "sequence": 1,
             "speaker": "user", "text": "  Help with my delivery?  "},
            {"message_id": "terminal_outcome:00000000000000000000000000000001",
             "request_id": "00000000-0000-0000-0000-000000000001", "sequence": 2,
             "speaker": "assistant", "text": "Stored answer 1", "outcome_kind": "completed_answer"},
        ])
        self.assertEqual(json.loads(response.model_dump_json())["notices"], [])

    def test_mixed_notices_merge_by_terminal_time_then_historical_request(self):
        # Sorting offer creation or interleaving by message sequence would break this order.
        from uuid import UUID
        from tests.test_records import transcript_marker, transcript_offer
        answer = transcript_pair(9, 1, with_offer=True)
        failed = transcript_pair(2, 3, "failed_technical", with_offer=True)
        refusal = transcript_pair(1, 5, "completed_refusal", with_offer=True)
        answer[1]["metadatas"][0]["created_at"] = "2026-11-02T10:03:00Z"
        refusal[1]["metadatas"][0]["created_at"] = "2026-11-02T10:02:00Z"
        failed[1]["metadatas"][0]["created_at"] = "2026-11-02T10:02:00Z"
        failed[0]["documents"][0] = "SECRET failed question"
        failed[1]["metadatas"][0]["cleared_citation_ids"] = ["SECRET citation"]
        security = transcript_marker(3, created_at="2026-11-02T10:01:00Z")
        dependency = transcript_marker(4, created_at="2026-11-02T10:04:00Z")
        dependency["metadatas"][0]["reason_code"] = "dependency_failure"
        offers = {str(UUID(int=n + 1000)): transcript_offer(n, reason=reason) for n, reason in [
            (1, "individual_case"), (2, "individual_case"), (3, "security_uncertain"),
            (4, "dependency_failure"), (9, "individual_case"),
        ]}
        offers[str(UUID(int=1009))]["metadatas"][0]["created_at"] = "2026-11-02T09:00:00Z"
        transcript = self.verified(*refusal, dependency, *failed, security, *answer, offers=offers)
        payload = json.loads(self.project(transcript).model_dump_json())
        self.assertEqual(payload["notices"], [
            {"request_id": "00000000-0000-0000-0000-000000000003", "reason_code": "security_uncertain",
             "offer_id": "00000000-0000-0000-0000-0000000003eb",
             "code_owned_text": "The request could not be completed safely."},
            {"request_id": "00000000-0000-0000-0000-000000000001", "reason_code": "individual_case",
             "offer_id": "00000000-0000-0000-0000-0000000003e9",
             "code_owned_text": "A simulated support offer was saved for this exchange."},
            {"request_id": "00000000-0000-0000-0000-000000000002", "reason_code": "technical_error",
             "offer_id": None, "code_owned_text": "The operation failed."},
            {"request_id": "00000000-0000-0000-0000-000000000009", "reason_code": "individual_case",
             "offer_id": "00000000-0000-0000-0000-0000000003f1",
             "code_owned_text": "A simulated support offer was saved for this exchange."},
            {"request_id": "00000000-0000-0000-0000-000000000004", "reason_code": "dependency_failure",
             "offer_id": "00000000-0000-0000-0000-0000000003ec",
             "code_owned_text": "The request could not be completed."},
        ])
        self.assertEqual([message["sequence"] for message in payload["messages"]], [1, 2, 5, 6])
        self.assertEqual([message["text"] for message in payload["messages"]], [
            "  Help with my delivery?  ", "Stored answer 9", "  Help with my delivery?  ", "Stored answer 1",
        ])
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertEqual(set(payload), {"request_id", "user_id", "conversation_id", "messages", "notices"})
        self.assertEqual(payload["request_id"], READ_ID)

    def test_all_completed_kinds_keep_normal_messages_with_linked_offer_notices(self):
        # Suppressing completed_offer messages or attaching offers only to that kind is wrong.
        from uuid import UUID
        from tests.test_records import transcript_offer
        rows, offers = [], {}
        for number, kind in enumerate([
            "completed_answer", "completed_clarification", "completed_refusal", "completed_offer",
        ], 1):
            rows.extend(transcript_pair(number, 2 * number - 1, kind, with_offer=True))
            offers[str(UUID(int=number + 1000))] = transcript_offer(number)
        payload = json.loads(self.project(self.verified(*rows, offers=offers)).model_dump_json())
        self.assertEqual([m["outcome_kind"] for m in payload["messages"] if m["speaker"] == "assistant"], [
            "completed_answer", "completed_clarification", "completed_refusal", "completed_offer",
        ])
        self.assertEqual(len(payload["messages"]), 8)
        self.assertEqual(len(payload["notices"]), 4)

    def test_full_fifty_event_history_remains_readable(self):
        # Applying admission's plus-two requirement to a read would incorrectly reject this.
        rows = [row for number in range(1, 26) for row in transcript_pair(number, 2 * number - 1)]
        transcript = self.verified(*reversed(rows))
        self.assertFalse(transcript.has_two_event_room)
        payload = json.loads(self.project(transcript).model_dump_json())
        self.assertEqual(len(payload["messages"]), 50)
        self.assertEqual(payload["messages"][-1]["sequence"], 50)
        self.assertEqual(payload["messages"][-1]["text"], "Stored answer 25")

    def test_invalid_selectors_fail_before_projection(self):
        # Removing strict native validation would allow invalid correlation or coerced identity.
        from pydantic import ValidationError
        for changes in [
            {"user_id": "../foreign"}, {"user_id": 1}, {"conversation_id": "invalid"},
            {"conversation_id": "{01234567-89ab-4cde-8fab-0123456789ab}"},
            {"request_id": ""}, {"request_id": 1},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.project(self.verified(), **changes)

    def test_record_bindings_cannot_be_relabelled_with_other_valid_selectors(self):
        # Echoing fresh valid selectors over a nonempty transcript would rebind historical content.
        from tests.test_records import transcript_marker, transcript_offer
        histories = [self.verified(*transcript_pair()), self.verified(
            transcript_marker(), offers={"00000000-0000-0000-0000-0000000003e9":
                                         transcript_offer(reason="security_uncertain")},
        )]
        for transcript in histories:
            for changes in [{"user_id": "syn-cust-02"},
                            {"conversation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}]:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.project(transcript, **changes)

    def test_equivalent_uuid_and_detached_immutable_output(self):
        # Retaining mutable input aliases or returning a noncanonical conversation breaks correlation.
        from pydantic import ValidationError
        pending, terminal = transcript_pair()
        transcript = self.verified(pending, terminal)
        response = self.project(transcript, conversation_id=CONVERSATION_ID.replace("-", "").upper(),
                                request_id="FFFFFFFFFFFF4FFF8FFFFFFFFFFFFFFF")
        pending["documents"][0] = "later input mutation"
        payload = json.loads(response.model_dump_json())
        self.assertEqual(payload["conversation_id"], "01234567-89ab-4cde-8fab-0123456789ab")
        self.assertEqual(payload["request_id"], "FFFFFFFFFFFF4FFF8FFFFFFFFFFFFFFF")
        self.assertEqual(payload["messages"][0]["text"], "  Help with my delivery?  ")
        payload["messages"][0]["text"] = "later output mutation"
        self.assertEqual(response.messages[0].text, "  Help with my delivery?  ")
        with self.assertRaises(ValidationError):
            response.user_id = "syn-cust-02"
        with self.assertRaises(ValidationError):
            response.messages[0].text = "replacement"

    def test_verified_utf8_limit_text_is_preserved_without_truncation(self):
        # Character-count truncation or ASCII conversion would change legal stored content.
        pending, terminal = transcript_pair()
        pending["documents"][0] = "🚀" * 2048
        terminal["documents"][0] = "é" * 4096
        response = self.project(self.verified(pending, terminal))
        payload = json.loads(response.model_dump_json())
        self.assertEqual(payload["messages"][0]["text"], "🚀" * 2048)
        self.assertEqual(payload["messages"][1]["text"], "é" * 4096)
