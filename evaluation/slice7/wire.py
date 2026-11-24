# Python 3.12 / httpx2 2.16.0; Slice 7 LOCAL evaluation — provider call recording and pricing (design v3 §6-8)
# The OpenAI and TypeSafe clients get this transport: it blocks non-reviewed endpoints/models before sending,
# saves each call privately, prices its native usage at dated rates and adds it to the shared ledger.
# Pricing rules are the Slice 4 V4 driver's estimate(), whose embedding figure matched the OpenAI dashboard.
import json
import os
import time
from decimal import Decimal
from threading import Lock

import httpx2  # Transport seam of the OpenAI 3.46 and TypeSafe 0.7.5 clients.

# Declared standard public prices (USD per 1M tokens): checked 2026-11-19 (V4) rechecked unchanged on 2026-11-22 and 2026-11-24 (OpenAI; gpt-6.2-sol added)
# at developers.openai.com/api/docs/pricing (standard, <=272K input) and help.typesafe.ai (Jev; no version named).
RATES = {'checked_on': '2026-11-24',  # Official pricing page; gpt-6.1-sol unchanged, gpt-6.2-sol added (same prices).
         'models': {'gpt-6.1-sol': {'input': '2', 'output': '10', 'cached': '.10', 'write_30m': '2.50', 'write_12h': '5'},
                    'gpt-6.2-sol': {'input': '2', 'output': '10', 'cached': '.10', 'write_30m': '2.50', 'write_12h': '5'},
                    'gpt-6-luna': {'input': '.10', 'output': '.50', 'cached': '.01', 'write_30m': '.125',
                                   'write_12h': '.25'},
                    'gpt-6.1-terra': {'input': '1', 'output': '5', 'cached': '.05', 'write_30m': '1.25',
                                      'write_12h': '2.50'},
                    'jev-1.14.0': {'input': '.042', 'output': '0'},
                    'text-embedding-3-small': {'input': '.02', 'output': '0'}}}
ALLOWED = {('api.openai.com', '/v1/responses'): {'gpt-6.1-sol', 'gpt-6.2-sol', 'gpt-6-luna', 'gpt-6.1-terra'},
           ('api.openai.com', '/v1/embeddings'): {'text-embedding-3-small'},
           ('api.typesafe.ai', '/v1/systemone'): {'jev-1.14.0'}}
MILLION = Decimal(1000000)


def write_private_json(path, data, *, indent=None):
    """Owner-only (mode 600) JSON file, replaced on rewrite: run files may hold app and judge text."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=indent)


def _count(value):
    return type(value) is int and value >= 0


def price(model, usage, service_tier=None):
    """USD for one call from its native usage, or None when anything needed is missing or inconsistent."""
    rate = RATES['models'].get(model)
    if not rate or not isinstance(usage, dict) or service_tier not in (None, 'default', 'standard'):
        return None
    if model == 'text-embedding-3-small':
        tokens = usage.get('prompt_tokens')
        return Decimal(tokens) * Decimal(rate['input']) / MILLION if _count(tokens) else None
    if model == 'jev-1.14.0':
        tokens = usage.get('input_tokens')
        if not _count(tokens) or not _count(usage.get('output_tokens')):
            return None  # Output is documented free, but a missing counter is incomplete usage.
        return Decimal(tokens) * Decimal(rate['input']) / MILLION
    tokens, output, details = usage.get('input_tokens'), usage.get('output_tokens'), usage.get('input_tokens_details')
    if not _count(tokens) or tokens > 272000 or not _count(output) or not isinstance(details, dict):
        return None
    cached, written = details.get('cached_tokens'), details.get('cache_write_tokens')
    if not _count(cached) or not _count(written) or cached + written > tokens:
        return None
    write_cost = Decimal(0)
    if written:
        split = details.get('cache_write_tokens_details')
        short = split.get('ttl_30m_tokens') if isinstance(split, dict) else None
        long = split.get('ttl_12h_tokens') if isinstance(split, dict) else None
        if not _count(short) or not _count(long) or short + long != written:
            return None
        write_cost = Decimal(short) * Decimal(rate['write_30m']) + Decimal(long) * Decimal(rate['write_12h'])
    total = usage.get('total_tokens')
    if total is not None and total != tokens + output:
        return None
    # Cached and cache-written input are subsets of input; reasoning is inside output_tokens (charged once).
    return (Decimal(tokens - cached - written) * Decimal(rate['input']) + Decimal(cached) * Decimal(rate['cached'])
            + write_cost + Decimal(output) * Decimal(rate['output'])) / MILLION


class WireLog:
    """All provider calls of one case (or of the judge), kept privately and priced into the shared ledger."""

    def __init__(self, directory, *, ledger, kind='app'):
        self.directory, self.ledger, self.kind = directory, ledger, kind
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.calls, self.lock = [], Lock()

    def save(self, call):
        write_private_json(os.path.join(self.directory, 'wire-%03d.json' % call['attempt']), call)

    def provider_errors(self):
        return sum(1 for call in self.calls if (call['http_status'] or 0) >= 400)  # Completed HTTP errors only.


class RecordingTransport(httpx2.BaseTransport):
    def __init__(self, log, inner):
        self.log, self.inner = log, inner

    def handle_request(self, request):
        body = json.loads(request.content or b'{}')
        model = body.get('model')
        if (request.method != 'POST' or request.url.scheme != 'https' or request.url.port not in (None, 443)
                or model not in ALLOWED.get((request.url.host, request.url.path), set())):
            raise ValueError('Provider endpoint/model is outside the reviewed allowlist.')  # Nothing sent.
        with self.log.lock:
            call = {'attempt': len(self.log.calls) + 1, 'endpoint': request.url.path, 'model': model,
                    'request': body, 'http_status': None, 'usage': None, 'usd': None, 'elapsed_ms': None}
            self.log.calls.append(call)
        started = time.perf_counter()
        try:
            response = self.inner.handle_request(request)
            raw = response.read()
            call['http_status'] = response.status_code
            try:
                payload = json.loads(raw)
                call['usage'], call['response_model'] = payload.get('usage'), payload.get('model')
                call['service_tier'] = payload.get('service_tier')
            except (ValueError, AttributeError):
                payload = None
            return response
        except BaseException as error:
            call['transport_error'] = type(error).__name__  # Type only: messages may carry private text.
            raise
        finally:
            call['elapsed_ms'] = (time.perf_counter() - started) * 1000
            cost = price(call.get('response_model') or model, call['usage'], call.get('service_tier'))
            call['usd'] = None if cost is None else str(cost)
            self.log.ledger.record(self.log.kind, cost)  # Every attempt counts; unknown when unpriced.
            self.log.save(call)

    def close(self):
        self.inner.close()


def recording_http(log, inner=None):
    """HTTP client for a provider SDK: V4 driver settings (no proxy/env config, no redirects, no retries)."""
    network = inner if inner is not None else httpx2.HTTPTransport(retries=0, trust_env=False)
    return httpx2.Client(transport=RecordingTransport(log, network), trust_env=False, follow_redirects=False)


def recording_openai(log, *, api_key, inner=None):
    """OpenAI client on the recording transport, with hosted.build_app's fixed endpoint and no SDK retry."""
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url='https://api.openai.com/v1', max_retries=0,
                  http_client=recording_http(log, inner))


# Flow: SDK request -> allowlist (else ValueError, nothing sent) -> real transport -> raw usage/model/status ->
# dated price (or unknown) -> shared ledger -> private wire-NNN.json for the route trace and audit.
