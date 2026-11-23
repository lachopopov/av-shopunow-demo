# Chroma 1.5.9 / Python 3.12; controlled async collection boundary, no network.
import asyncio
import unittest
from uuid import UUID
from shopunow.sdk_writes import SDKRecords
from shopunow.records import verify_handoff_offer, verify_preclearance_terminal
from tests.test_records import (OFFER_ID, REQUEST_ID, USER_ID, CONVERSATION_ID,
                               offer_result, marker_result)


class ImmediateBridge:
    def call(self, operation, *, timeout):
        return asyncio.run(operation())  # Test-only owner; production uses its fixed SDK loop.


class CapturedCollection:
    def __init__(self):
        self.calls = []

    async def add(self, **payload):
        self.calls.append(payload)  # This is the external storage boundary only.


class PreclearanceSDKTests(unittest.TestCase):
    def setUp(self):
        self.conversations, self.escalations = CapturedCollection(), CapturedCollection()
        self.records = SDKRecords(ImmediateBridge(), profiles=None,
            conversations=self.conversations, escalations=self.escalations)

    def test_contact_free_offer_maps_to_existing_native_record_contract(self):
        write = getattr(self.records, 'write_offer', None)
        self.assertTrue(callable(write), 'Native offer writer is missing')
        raw = offer_result()
        offer = verify_handoff_offer(raw, user_id=USER_ID, conversation_id=CONVERSATION_ID,
            request_id=REQUEST_ID, offer_id=OFFER_ID, reason_code='individual_case',
            target_department_or_fallback='shipping_delivery').record
        write(offer, timeout=2)
        payload = self.escalations.calls[0]
        self.assertEqual(payload['ids'], ['offer:' + UUID(OFFER_ID).hex])
        self.assertEqual(payload['documents'], ['handoff_offer'])
        self.assertEqual(payload['metadatas'], raw['metadatas'])
        self.assertEqual(payload['embeddings'], [[1.0, 0.0, 0.0]])
        self.assertEqual(self.conversations.calls, [])

    def test_marker_has_fixed_document_no_question_or_normal_message_fields(self):
        write = getattr(self.records, 'write_preclearance_marker', None)
        self.assertTrue(callable(write), 'Native marker writer is missing')
        raw = marker_result()
        marker = verify_preclearance_terminal(raw, user_id=USER_ID,
            conversation_id=CONVERSATION_ID, request_id=REQUEST_ID, offer_id=OFFER_ID).record
        write(marker, timeout=2)
        payload = self.conversations.calls[0]
        self.assertEqual(payload['ids'], raw['ids'])
        self.assertEqual(payload['documents'], ['preclearance_terminal'])
        self.assertEqual(payload['metadatas'], raw['metadatas'])
        self.assertEqual(self.escalations.calls, [])


# Flow: real record verification -> SDK mapping -> captured native add payload.
