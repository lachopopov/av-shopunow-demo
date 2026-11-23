# Python 3.12 / Promptfoo 0.124.1 python provider (call_api); Slice 7 LOCAL evaluation (design v3 §6-7)
# Promptfoo's Python provider is a persistent worker, so this file only binds the case to the dataset, reserves
# budget, starts ONE fresh subprocess for the case (CD-044) and maps its JSON to Promptfoo's response fields.
# Credentials reach the process through `uv run --env-file .env` (run.sh); nothing here reads .env or prints them.
import os
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # Promptfoo's worker does not put this folder on the import path.

from case_runner import run_in_subprocess  # noqa: E402 - one fresh process per case.
from cases import rows  # noqa: E402
from ledger import Ledger  # noqa: E402
import two_turn  # noqa: E402

REAL_BUILDER = 'app_builder:real_build'
CASE_TIMEOUT_SECONDS = 600  # Above the MLD §11 budget (15 s + 90 s) per turn plus app start/stop; explicit.


def case_spec(prompt, variables, *, run_dir, token, builder):
    """Subprocess spec: the dataset query and profile only, bound to the case ID; nothing evaluator-side."""
    if variables.get('case_id') == two_turn.CASE_ID:  # Both turns and the fictitious contact (design §3a).
        case = two_turn.kept()
        query, profile, extra = case['turn1'], case['profile_id'], {'handoff': dict(case['contact'])}
        turns = [case['turn1'], case['turn2']]
    else:
        row = rows().get(variables.get('case_id')) or {}
        query, profile, extra = row.get('query'), row.get('__metadata:profile_id'), {}
        turns = [query]
    if query is None or prompt != query or variables.get('profile_id') != profile:
        raise ValueError('Prompt/profile differs from the dataset row for this case.')
    folder = run_dir / 'cases' / ('%s-a%s' % (variables['case_id'], variables['attempt']))
    return {'builder': builder, 'token': token, 'user_id': profile, 'turns': turns,
            'wire_dir': str(folder / 'wire'), 'ledger': str(run_dir / 'ledger.json'), **extra}


def respond(result):
    """Subprocess JSON -> Promptfoo response; cost/tokenUsage only when every call's usage is known."""
    calls = result['wire_calls']
    unknown = sum(1 for call in calls if call['usd'] is None)
    if result.get('setup_failed'):  # POST /conversations failed: an invalid setup, graded as such (M4).
        return {'output': {'setup_failed': True, 'http_status': result['conversation']['status'],
                           'calls': [], 'unknown_cost_calls': unknown, 'trace': result['trace']}}
    turn = result['turns'][-1]
    body = turn['body'] or {}  # A raw 5xx has no JSON body; it is still graded (M4).
    outcome = body.get('outcome') or {}
    output = {'http_status': turn['status'], 'error_code': (body.get('error') or {}).get('code'),
              'kind': outcome.get('kind'), 'text': outcome.get('text', ''), 'offer_id': outcome.get('offer_id'),
              'reason_code': result['trace']['offer_reason'], 'trace': result['trace'],
              # Only completed HTTP errors are provider errors (invalid run); timeouts and other transport errors
              # are graded by the app's own outcome and listed for triage (challenger H1).
              'provider_errors': sum(1 for call in calls if (call['http_status'] or 0) >= 400),
              'transport_errors': [call['transport_error'] for call in calls if call.get('transport_error')],
              'calls': [{key: call.get(key) for key in ('endpoint', 'model', 'http_status', 'usd', 'usage')}
                        for call in calls],
              'unknown_cost_calls': unknown, 'latency_ms': turn['latency_ms']}
    if len(result['turns']) > 1:  # Two-turn case: turn 1 decides determinacy (design §3a).
        first = result['turns'][0]
        first_outcome = (first['body'] or {}).get('outcome') or {}
        output['first_turn'] = {'http_status': first['status'], 'kind': first_outcome.get('kind'),
                                'text': first_outcome.get('text', ''), 'offer_id': first_outcome.get('offer_id'),
                                'error_code': ((first['body'] or {}).get('error') or {}).get('code')}
    if result.get('handoff') is not None:
        output['handoff'] = {'status': result['handoff']['status'],
                             'receipt_id': (result['handoff']['body'] or {}).get('receipt_id')}
    response = {'output': output, 'latencyMs': round(turn['latency_ms'])}  # Last POST /interactions only.
    if unknown == 0:
        response['cost'] = float(sum((Decimal(call['usd']) for call in calls), Decimal(0)))
        prompt = sum(call['usage'].get('input_tokens', call['usage'].get('prompt_tokens', 0)) for call in calls)
        completion = sum(call['usage'].get('output_tokens', 0) for call in calls)
        response['tokenUsage'] = {'prompt': prompt, 'completion': completion, 'total': prompt + completion,
                                  'numRequests': len(calls)}
    return response


def call_api(prompt, options, context):
    """Promptfoo entry: budget check -> one case subprocess -> response; errors carry no private text."""
    try:
        variables = context['vars']
        run_dir = Path(os.environ['SLICE7_RUN_DIR'])
        if not Ledger(run_dir / 'ledger.json').reserve_case('%s#%s' % (variables['case_id'], variables['attempt'])):
            return {'error': 'BUDGET_STOP: not run (spent + $0.40 reservation would exceed $5)'}
        spec = case_spec(prompt, variables, run_dir=run_dir, token=os.environ['SHOPUNOW_API_TOKEN'],
                         builder=options.get('config', {}).get('builder', REAL_BUILDER))
        return respond(run_in_subprocess(spec, timeout=CASE_TIMEOUT_SECONDS))
    except Exception as error:  # Type only: messages may carry private text.
        return {'error': 'CASE_FAILED: ' + type(error).__name__}


# Flow: Promptfoo row (query + profile_id + case_id/attempt) -> dataset binding check -> ledger reservation ->
# case subprocess (app_builder, lifespan, POSTs) -> output JSON (outcome, trace, calls) + latencyMs + cost/tokens.
