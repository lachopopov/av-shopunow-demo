# Pydantic 2.14.1 / FastAPI 0.143.2; wire checks alone confer no workflow permission.
import json
import unittest
import httpx
from pydantic import ValidationError
from shopunow import schemas, api
from tests.test_records import USER_ID, CONVERSATION_ID

RID='ffffffff-ffff-4fff-8fff-ffffffffffff'
OFFER='22222222-2222-4222-8222-222222222222'
SUBMISSION='33333333-3333-4333-8333-333333333333'
RECEIPT='44444444-4444-4444-8444-444444444444'


class RemainingWireTests(unittest.TestCase):
    def response(self,outcome,**extra):
        self.assertTrue(hasattr(schemas,'InteractionResponse'),'Missing tagged interaction wire contract')
        return schemas.InteractionResponse.model_validate_json(json.dumps({'user_id':USER_ID,
            'conversation_id':CONVERSATION_ID,'request_id':RID,'outcome':outcome,**extra}))

    def test_six_outcome_tags_have_distinct_native_wire_shapes(self):
        outcomes=[{'kind':'answer','text':'Reviewed answer.','cited_ids':['knowledge-1']},
                  {'kind':'clarification','text':'Which order?'},
                  {'kind':'refusal','text':'That information is restricted.'},
                  {'kind':'handoff_offer','offer_id':OFFER,'text':'Our support team can help with this.'},  # LLD 2026-11-21.
                  {'kind':'confirmed_attack_block','text':'The request cannot be processed.'},
                  {'kind':'fallback_error','text':'The request could not be completed safely.','offer_id':None}]
        for outcome in outcomes:
            with self.subTest(kind=outcome['kind']):
                body=json.loads(self.response(outcome).model_dump_json())
                self.assertEqual(body['outcome'],outcome)
                self.assertEqual(set(body),{'request_id','user_id','conversation_id','outcome'})

    def test_tags_do_not_accept_other_outcomes_fields_or_unknown_kinds(self):
        invalid=[{'kind':'answer','text':'x','cited_ids':[],'offer_id':OFFER},
                 {'kind':'answer','text':'x'}, {'kind':'handoff_offer'},
                 {'kind':'refusal','text':'x','receipt_id':RECEIPT},
                 {'kind':'unknown','text':'x'}, {'text':'x'},
                 {'kind':'confirmed_attack_block','text':'PRIVATE model output'},
                 {'kind':'fallback_error','text':'PRIVATE model output','offer_id':None}]
        for outcome in invalid:
            with self.subTest(outcome=outcome):
                self.assertTrue(hasattr(schemas,'InteractionResponse'),'Missing interaction response')
                with self.assertRaises(ValidationError):
                    self.response(outcome)

    def test_wire_text_bounds_and_encoding_are_not_silent_truncation(self):
        for text in [' ', '\ud800', 'x'*8193, '😀'*2049]:
            with self.subTest(length=len(text)):
                self.assertTrue(hasattr(schemas,'InteractionResponse'),'Missing interaction response')
                with self.assertRaises((ValidationError,UnicodeError)):
                    self.response({'kind':'answer','text':text,'cited_ids':[]})
        answer=self.response({'kind':'answer','text':'😀'*2048,'cited_ids':[]})
        self.assertEqual(len(answer.outcome.text.encode('utf-8')),8192)

    def handoff(self,**changes):
        self.assertTrue(hasattr(schemas,'HandoffRequest'),'Missing handoff wire contract')
        body={'user_id':USER_ID,'conversation_id':CONVERSATION_ID,'offer_id':OFFER,
              'name':'Synthetic Person','phone':None,'email':'synthetic@example.test'}
        body.update(changes)
        return schemas.HandoffRequest.model_validate(body)

    def test_handoff_requires_name_and_at_least_one_valid_contact(self):
        for changes in [{'name':' '},{'phone':None,'email':None},{'email':'not-an-email'},
                        {'email':'x@example.test\nBcc:unsafe@example.test'}, {'phone':' '},
                        {'name':12}, {'phone':12}, {'target_department':'hr'}]:
            with self.subTest(changes=changes):
                self.assertTrue(hasattr(schemas,'HandoffRequest'),'Missing handoff request')
                with self.assertRaises(ValidationError):
                    self.handoff(**changes)
        self.assertIsNone(self.handoff(phone='synthetic-phone',email=None).email)
        self.assertEqual(self.handoff().email,'synthetic@example.test')

    def test_malformed_mailboxes_fail_as_native_validation_errors(self):
        for address in ['a@@example.test','a b@example.test','a@','@example.test']:
            with self.subTest(address=address):
                with self.assertRaises(ValidationError):
                    self.handoff(email=address)

    def test_email_followup_requirement_comes_from_trusted_validation_context(self):
        self.assertTrue(hasattr(schemas,'HandoffRequest'),'Missing handoff request')
        body={'user_id':USER_ID,'conversation_id':CONVERSATION_ID,'offer_id':OFFER,
              'name':'Synthetic Person','phone':'synthetic-phone'}
        self.assertIsNone(schemas.HandoffRequest.model_validate(body).email)
        with self.assertRaises(ValidationError):
            schemas.HandoffRequest.model_validate(body,context={'email_followup_required':True})
        with self.assertRaises(ValidationError):
            schemas.HandoffRequest.model_validate({**body,'email_followup_required':False})

    def test_receipt_is_explicitly_simulated_and_has_no_contact_payload(self):
        self.assertTrue(hasattr(schemas,'HandoffResponse'),'Missing receipt wire contract')
        body={'user_id':USER_ID,'conversation_id':CONVERSATION_ID,'request_id':RID,
              'submission_id':SUBMISSION,'receipt_id':RECEIPT,'contact_method':'email','simulated':True}
        self.assertEqual(json.loads(schemas.HandoffResponse.model_validate(body).model_dump_json()),body)
        for changes in [{'simulated':False},{'simulated':1},{'simulated':'true'},
                        {'contact_method':'staff_assignment'},{'email':'private@example.test'},
                        {'receipt_id':'bad-id'}]:
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    schemas.HandoffResponse.model_validate({**body, **changes})

    def test_fallback_offer_reference_is_shape_only_not_proof_of_saved_offer(self):
        response=self.response({'kind':'fallback_error','text':'The request could not be completed safely.',
                                'offer_id':OFFER})
        self.assertEqual(response.outcome.offer_id,OFFER)
        # No fabricated proof field may bypass the later exact linked-record checks.
        with self.assertRaises(ValidationError):
            self.response({'kind':'handoff_offer','offer_id':OFFER,'verified':True})


class HealthWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_static_liveness_without_token_or_storage_routes(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.create_app()),base_url='http://test') as client:
            response=await client.get('/health')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json(),{'status':'ok'})
            for path in ['/interactions','/handoffs']:
                self.assertEqual((await client.post(path,json={})).status_code,404)
