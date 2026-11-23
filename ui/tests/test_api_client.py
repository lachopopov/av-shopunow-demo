# HTTPX 0.28.1; the UI client against a controlled backend (httpx.MockTransport), no network.
import json
import unittest
import httpx
import api_client

TOKEN = 'server-side-token'
USER, CONV = 'syn-cust-01', '01234567-89ab-4cde-8fab-0123456789ab'
RID = 'ffffffff-ffff-4fff-8fff-ffffffffffff'


def client(handler):
    return api_client.Client('http://backend.test', TOKEN, transport=httpx.MockTransport(handler))


class ClientTests(unittest.TestCase):
    def test_every_request_carries_the_server_side_token(self):
        # Break caught: a backend call without the token, or the token placed where a browser sees it.
        seen = []
        def handler(request):
            seen.append(request.headers.get('x-api-token'))
            return httpx.Response(200, json={'request_id': RID, 'user_id': USER, 'conversations': []})
        client(handler).list_conversations(USER)
        self.assertEqual(seen, [TOKEN])

    def test_an_answer_bound_to_another_user_or_conversation_is_rejected(self):
        # Break caught: a response for a different selection is shown as this user's answer.
        def handler(request):
            return httpx.Response(200, json={'request_id': RID, 'user_id': 'syn-cust-02', 'conversation_id': CONV,
                                             'outcome': {'kind': 'refusal', 'text': 'x'}})
        result = client(handler).ask(USER, CONV, 'Question?', None)
        self.assertFalse(result.ok)
        self.assertTrue(result.uncertain)  # A question may have been saved; never retried automatically.

    def test_a_code_owned_capacity_503_is_a_definite_not_started(self):
        # Break caught: a validated pre-execution rejection reported as an uncertain save.
        def handler(request):
            return httpx.Response(503, json={'error': {'code': 'execution_capacity',
                'message': 'Execution capacity is unavailable.', 'request_id': RID}})
        result = client(handler).ask(USER, CONV, 'Question?', None)
        self.assertEqual((result.ok, result.uncertain, result.code), (False, False, 'execution_capacity'))

    def test_a_capacity_code_on_a_non_503_status_is_not_trusted_as_not_started(self):
        # Break caught (review): any status carrying the capacity code treated as proof nothing started.
        def handler(request):
            return httpx.Response(500, json={'error': {'code': 'execution_capacity', 'message': 'x',
                                                       'request_id': RID}})
        self.assertTrue(client(handler).ask(USER, CONV, 'Question?', None).uncertain)

    def test_an_unrecognised_503_or_a_timeout_leaves_a_write_uncertain(self):
        # Break caught: a host/proxy 503 or a lost connection treated as proof nothing was saved.
        def html_503(request):
            return httpx.Response(503, text='<html>Service Unavailable</html>')
        def timeout(request):
            raise httpx.ReadTimeout('timed out', request=request)
        for handler in (html_503, timeout):
            with self.subTest(handler.__name__):
                result = client(handler).ask(USER, CONV, 'Question?', None)
                self.assertEqual((result.ok, result.uncertain), (False, True))
                self.assertNotIn('Service Unavailable', result.message)  # Never echo unknown upstream text.

    def test_an_unknown_error_code_gets_a_fixed_message_not_the_backend_text(self):
        def handler(request):
            return httpx.Response(409, json={'error': {'code': 'something_new', 'message': 'PRIVATE detail',
                                                       'request_id': RID}})
        result = client(handler).list_conversations(USER)
        self.assertNotIn('PRIVATE', result.message)

    def test_the_question_wait_covers_the_90_second_backend_deadline_plus_margin(self):
        # Break caught: the UI gives up before the backend's 90 s deadline (MLD §11).
        timeouts = []
        def handler(request):
            timeouts.append(request.extensions['timeout'])
            return httpx.Response(200, json={'request_id': RID, 'user_id': USER, 'conversation_id': CONV,
                                             'outcome': {'kind': 'refusal', 'text': 'x'}})
        client(handler).ask(USER, CONV, 'Question?', 'brief')
        self.assertGreaterEqual(timeouts[0]['read'], 15 + 90 + 10)  # Pre-admission + question + margin.

    def test_an_uncertain_handoff_tells_the_user_to_resubmit_the_same_details(self):
        # Break caught (challenger D4): "reopen to check" although history shows no submission state.
        def timeout(request):
            raise httpx.ReadTimeout('timed out', request=request)
        result = client(timeout).submit_handoff(USER, CONV, RID, name='Sam', phone='', email='sam@example.test')
        self.assertTrue(result.uncertain)
        self.assertIn('same details again', result.message)
        self.assertNotIn('reopen', result.message)

    def test_handoff_receipt_is_returned_only_for_this_user_and_conversation(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body, {'user_id': USER, 'conversation_id': CONV, 'offer_id': RID,
                                    'name': 'Sam', 'email': 'sam@example.test'})
            return httpx.Response(200, json={'request_id': RID, 'user_id': USER, 'conversation_id': CONV,
                'submission_id': RID, 'receipt_id': RID, 'contact_method': 'email', 'simulated': True})
        result = client(handler).submit_handoff(USER, CONV, RID, name='Sam', phone='', email='sam@example.test')
        self.assertTrue(result.ok)
        self.assertEqual(result.body['receipt_id'], RID)


class BindingTests(unittest.TestCase):
    def test_only_items_from_the_current_selection_are_visible(self):
        # Break caught: a late answer or receipt from an earlier profile/conversation shows up after a switch.
        old, current = (USER, CONV, 1), (USER, CONV, 2)
        items = [{'sel': old, 'text': 'late'}, {'sel': current, 'text': 'now'},
                 {'sel': ('syn-cust-02', CONV, 2), 'text': 'other user'}]
        self.assertEqual([row['text'] for row in api_client.visible(items, current)], ['now'])



class NoticeTests(unittest.TestCase):
    """The pending notice appears only when a question takes longer than NOTICE_SECONDS."""

    def run_with(self, seconds):
        import time
        from concurrent.futures import ThreadPoolExecutor
        from unittest import mock
        shown = []
        with mock.patch.object(api_client, 'NOTICE_SECONDS', 0.2), ThreadPoolExecutor(1) as pool:
            future = pool.submit(lambda: time.sleep(seconds) or 'done')
            value = api_client.wait_with_notice(future, lambda: shown.append(True))
        return value, shown

    def test_a_slow_question_shows_the_notice_and_still_returns_its_result(self):
        # Break caught: no progress notice on a long wait, or the wait giving up at the notice.
        self.assertEqual(self.run_with(0.5), ('done', [True]))

    def test_a_fast_question_shows_no_notice(self):
        self.assertEqual(self.run_with(0.0), ('done', []))


if __name__ == '__main__':
    unittest.main()
