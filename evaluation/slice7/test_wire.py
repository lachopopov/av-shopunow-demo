# Python 3.12 / unittest / httpx2 2.16.0; Slice 7 LOCAL evaluation (design v3 §6 wire capture, §7-8 cost)
# Protects: every provider call is recorded with its real usage and priced correctly; calls outside the reviewed
# endpoints/models never leave the machine; missing usage is unknown, never $0; provider errors are visible.
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import httpx2  # The HTTP library the OpenAI 3.46 / TypeSafe 0.7.5 clients use (V4 driver seam).

from ledger import Ledger
from wire import RecordingTransport, WireLog, price, recording_openai, write_private_json  # Module under test (evaluation/slice7/wire.py).


def responses_usage(inputs, output, cached=0, written=0):
    return {'input_tokens': inputs, 'output_tokens': output, 'total_tokens': inputs + output,
            'input_tokens_details': {'cached_tokens': cached, 'cache_write_tokens': written},
            'output_tokens_details': {'reasoning_tokens': output // 2}}


class PriceTests(unittest.TestCase):
    def test_known_usage_is_priced_at_the_dated_rates(self):
        table = [  # Hand-computed with RATES checked 2026-11-19 (USD per 1M tokens).
            ('gpt-6.1-sol', responses_usage(1000, 500, cached=200), Decimal('0.00662')),  # 800*2+200*.1+500*10
            ('gpt-6-luna', responses_usage(2000, 1000), Decimal('0.0007')),               # 2000*.1+1000*.5
            ('gpt-6.1-terra', responses_usage(2500, 4000), Decimal('0.0225')),            # 2500*1+4000*5
            ('text-embedding-3-small', {'prompt_tokens': 1000, 'total_tokens': 1000}, Decimal('0.00002')),
            ('jev-1.14.0', {'input_tokens': 1000, 'output_tokens': 0}, Decimal('0.000042')),
        ]
        for model, usage, cost in table:
            with self.subTest(model=model):
                self.assertEqual(price(model, usage), cost)

    def test_missing_or_inconsistent_usage_is_unknown(self):
        for model, usage in (('gpt-6.1-sol', None), ('gpt-6.1-sol', {'input_tokens': 10}),
                             ('unknown-model', responses_usage(10, 10)),
                             ('gpt-6-luna', {**responses_usage(10, 10), 'total_tokens': 999})):
            with self.subTest(model=model, usage=usage):
                self.assertIsNone(price(model, usage))


def reply(model, usage, status=200):
    def handler(request):
        return httpx2.Response(status, json={'model': model, 'usage': usage, 'status': 'completed'})
    return handler


class RecordingTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.ledger = Ledger(self.root / 'ledger.json')
        self.log = WireLog(self.root / 'case', ledger=self.ledger)
        self.sent = []

    def tearDown(self):
        self.directory.cleanup()

    def client(self, handler):
        def counting(request):
            self.sent.append(request)
            return handler(request)
        return httpx2.Client(transport=RecordingTransport(self.log, httpx2.MockTransport(counting)))

    def test_allowed_call_is_recorded_priced_and_added_to_the_ledger(self):
        with self.client(reply('gpt-6-luna', responses_usage(2000, 1000))) as http:
            response = http.post('https://api.openai.com/v1/responses',
                                 json={'model': 'gpt-6-luna', 'input': '{"draft": "x"}'})
        self.assertEqual(response.status_code, 200)
        [call] = self.log.calls
        self.assertEqual((call['endpoint'], call['model'], call['http_status']), ('/v1/responses', 'gpt-6-luna', 200))
        self.assertEqual(call['request']['input'], '{"draft": "x"}')  # Body kept for the route trace.
        self.assertEqual(call['usd'], '0.0007')
        self.assertEqual(self.ledger.totals()['known_usd'], Decimal('0.0007'))
        self.assertTrue((self.root / 'case' / 'wire-001.json').exists())  # Private raw record per call.

    def test_call_outside_the_allowlist_never_leaves_the_machine(self):
        for url, body in (('https://api.openai.com/v1/responses', {'model': 'gpt-unreviewed'}),
                          ('https://example.com/v1/responses', {'model': 'gpt-6-luna'}),
                          ('http://api.openai.com/v1/responses', {'model': 'gpt-6-luna'})):
            with self.subTest(url=url), self.client(reply('x', {})) as http, self.assertRaises(ValueError):
                http.post(url, json=body)
        self.assertEqual(self.sent, [])

    def test_missing_usage_goes_to_the_ledger_as_unknown(self):
        with self.client(reply('gpt-6.1-sol', None)) as http:
            http.post('https://api.openai.com/v1/responses', json={'model': 'gpt-6.1-sol'})
        self.assertEqual(self.ledger.totals()['unknown_items'], 1)
        self.assertIsNone(self.log.calls[0]['usd'])

    def test_provider_http_error_is_counted(self):
        with self.client(reply('gpt-6.1-sol', None, status=429)) as http:
            http.post('https://api.openai.com/v1/responses', json={'model': 'gpt-6.1-sol'})
        self.assertEqual(self.log.provider_errors(), 1)
        self.assertEqual(json.loads((self.root / 'case' / 'wire-001.json').read_text())['http_status'], 429)


class SharedHelperTests(unittest.TestCase):
    """Protects: run files stay owner-only, and every evaluator OpenAI client records calls and never retries."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_private_json_is_owner_only_and_replaced_on_rewrite(self):
        path = self.root / 'verdict.json'
        write_private_json(path, {'first': 1, 'long': 'x' * 50})
        write_private_json(path, {'second': 2}, indent=1)
        self.assertEqual(json.loads(path.read_text()), {'second': 2})  # Truncated, not appended.
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_recording_openai_records_one_attempt_and_does_not_retry(self):
        sent = []

        def server_error(request):
            sent.append(request)
            return httpx2.Response(500, json={'error': {'message': 'x'}})
        log = WireLog(self.root / 'judge', ledger=Ledger(self.root / 'ledger.json'), kind='judge')
        client = recording_openai(log, api_key='test-key', inner=httpx2.MockTransport(server_error))
        with self.assertRaises(Exception):
            client.responses.create(model='gpt-6.1-terra', input='x')
        self.assertEqual(len(sent), 1)  # max_retries=0 and no transport retry (MLD §11: no blind retry).
        self.assertEqual([call['http_status'] for call in log.calls], [500])
        self.assertEqual(str(sent[0].url), 'https://api.openai.com/v1/responses')


if __name__ == '__main__':
    unittest.main()

# Summary: hand-computed prices per model and usage shape; a recording transport that blocks non-reviewed
# endpoints/models before sending, saves each call privately, prices it into the shared ledger (unknown when
# usage is missing) and counts provider HTTP errors for the invalid-run rule.
