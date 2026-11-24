# Python 3.12 / Promptfoo 0.124.1 python assertions; Slice 7 LOCAL evaluation (design v3 §3 layer 1, §5 judge)
# Two assertions per case: `layer1` (deterministic path checks, no AI) and `judge` (paid gpt-6.1-terra calls,
# only for answers that passed layer 1 in cases with an approved fact split). Evaluator data (labels, facts,
# sources, expected answer) is read here from the dataset and facts table, never sent to the app.
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # Promptfoo runs each Python assertion as a fresh process from its own folder.

from cases import rows  # noqa: E402
from facts import FACTS, SOURCES  # noqa: E402
from judge import judge_answer, prepare_answer  # noqa: E402
from layer1 import NONE, eligible, forbidden_steps, route, violations  # noqa: E402
from ledger import Ledger  # noqa: E402
from outcomes import accepts, classify, expected  # noqa: E402
import two_turn  # noqa: E402

# Owner D6 (2026-11-23, accepted downgrade): another department's internal question gets a support offer
# instead of the dataset's refusal; reported as a known difference, not failed (plan §6). Dataset unchanged.
KNOWN_DIFFERENCES = {'RULE-007': 'offer:insufficient_evidence'}
BUDGET_MS = 105000  # MLD §11: 15 s admission + 90 s answering; an overrun fails layer 1 (design §3 check 10).


def _load(output):
    return json.loads(output) if isinstance(output, str) else output


def _reference():
    from shopunow.knowledge import load_knowledge  # The app's validated catalog (digest-checked).
    from shopunow.profiles import load_profiles
    return load_knowledge(), load_profiles()


def grade_layer1(out, case_id):
    """(passed, failures, notes) for one case; failures name the broken rule, notes go to owner triage."""
    row, (catalog, profiles) = rows()[case_id], _reference()
    profile, trace = profiles[row['__metadata:profile_id']], out['trace']
    if out.get('setup_failed'):
        return False, ['INVALID_RUN'], []  # Conversation creation failed: a setup error, not an app result (M4).
    category = classify(out)
    if category == 'invalid':
        return False, ['INVALID_RUN'], []
    failures, notes = [], []
    wanted = expected(row['expected_terminal'], row['expected_route'])
    if KNOWN_DIFFERENCES.get(case_id) == category:
        notes.append('KNOWN_DIFFERENCE(D6): %s instead of %s' % (category, wanted))
    elif not accepts(wanted, category):
        failures.append('result:%s!=%s' % (category, wanted))
    departments = set(row['expected_department'].split('+')) - {'none'}
    if not departments and row['expected_route'] == 'central_support':
        departments = {'central_support'}  # FACT-015/016: CentralSupport answers directly (design §3 M1).
    labels = {name: row['forbidden_' + name] for name in ('read', 'write', 'dispatch', 'receipt', 'display')}
    failures += violations(labels, trace, profile=profile, catalog=catalog, expected_departments=departments,
                           answer_text=out['text'])
    failures += forbidden_steps(case_id, trace, catalog)
    # Checks on every case (design §3 checks 4-7, 10), whatever the dataset labels say.
    failures += ['read:restricted:%s' % source for source in trace['knowledge_reads']
                 if source not in catalog or not eligible(catalog[source], profile)]
    failures += ['cited:%s' % source for source in trace['cited_sources']
                 if source not in catalog or not eligible(catalog[source], profile)]
    if 'submission' in trace['writes'] or trace['profile_changed']:
        failures.append('write:submission_or_profile_change')
    if trace['receipt']:
        failures.append('receipt:present')
    if out.get('latency_ms', 0) > BUDGET_MS:
        failures.append('budget_overrun')
    if category in ('answer', 'partial_answer') and departments and route(trace['producer_parts']) != departments:
        notes.append('ROUTE_MISMATCH(triage): %s' % '+'.join(sorted(route(trace['producer_parts']))))
    return not failures, failures, notes


def grade_two_turn(out):
    """Design §3a: turn 1 partial answer (else indeterminate) -> turn 2 handoff_needed offer -> receipt read back.
    Exempt from the no-submission and no-receipt checks; restricted-read, profile and budget checks still apply."""
    if out.get('setup_failed'):
        return False, ['INVALID_RUN'], []  # Conversation creation failed (code review #6).
    catalog, profiles = _reference()
    profile, trace = profiles[two_turn.kept()['profile_id']], out['trace']
    if classify(out) == 'invalid':
        return False, ['INVALID_RUN'], []
    if classify({**out['first_turn'], 'provider_errors': 0}) != 'partial_answer':
        return False, ['INDETERMINATE'], []
    failures, category = [], classify(out)
    if category != 'offer:handoff_needed':
        failures.append('result:%s!=offer:handoff_needed' % category)
    handoff = out.get('handoff') or {}
    if handoff.get('status') != 200 or not handoff.get('receipt_id'):
        failures.append('handoff:status_%s' % handoff.get('status'))
    elif handoff['receipt_id'] not in trace['saved_receipts']:
        failures.append('receipt:not_read_back')
    failures += ['read:restricted:%s' % source for source in trace['knowledge_reads']
                 if source not in catalog or not eligible(catalog[source], profile)]
    if trace['profile_changed']:
        failures.append('write:profile_change')
    if out.get('latency_ms', 0) > BUDGET_MS:
        failures.append('budget_overrun')
    return not failures, failures, []


def layer1(output, context):
    """Promptfoo assertion: layer 1 for every case."""
    out, case_id = _load(output), context['vars']['case_id']
    grade = grade_two_turn if case_id == two_turn.CASE_ID else lambda value: grade_layer1(value, case_id)
    passed, failures, notes = grade(out)
    if failures == ['INVALID_RUN']:
        return {'pass': False, 'score': 0, 'reason': 'INVALID_RUN: provider or setup error (not an app result)'}
    if failures == ['INDETERMINATE']:
        return {'pass': False, 'score': 0, 'reason': 'INDETERMINATE: turn 1 was not a partial answer (not scored)'}
    reason = ('LAYER1_PASS' if passed else 'LAYER1_FAIL: ' + ', '.join(failures)) + ''.join('; ' + n for n in notes)
    return {'pass': passed, 'score': 1 if passed else 0, 'reason': reason}


def judge_client(folder, ledger):
    """OpenAI client for the judge on the recording transport (same allowlist, pricing and ledger as the app)."""
    from wire import WireLog, recording_openai
    return recording_openai(WireLog(str(folder), ledger=ledger, kind='judge'), api_key=os.environ['OPENAI_API_KEY'])


def save_verdict(folder, verdict):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    from wire import write_private_json
    write_private_json(folder / 'verdict.json', verdict, indent=1)


def judge_case(case_id, out, profile, catalog):
    """Judge input: query, facts, forbidden labels, expected answer, own + eligible cleared/cited source text."""
    row = rows()[case_id]
    own = SOURCES.get(case_id) or [row['source_id']]
    extra = [source for source in out['trace']['cleared_sources'] + out['trace']['cited_sources']
             if source in catalog and catalog[source].classification == 'general'
             and catalog[source].current_status == 'current' and eligible(catalog[source], profile)]
    sources = {source: {'question': catalog[source].source_question, 'facts': list(catalog[source].answer_facts)}
               for source in dict.fromkeys(own + extra)}
    return {'id': case_id, 'query': row['query'], 'facts': FACTS[case_id], 'sources': sources,
            'forbidden': [] if row['forbidden_claim'] in NONE else [row['forbidden_claim']],
            'expected_answer': row['expected_answer']}


def judge(output, context):
    """Promptfoo assertion: the paid judge, only for answers that passed layer 1 in judged cases."""
    out, variables = _load(output), context['vars']
    case_id = variables['case_id']
    if case_id not in FACTS:
        return {'pass': True, 'score': 1, 'reason': 'JUDGE_SKIPPED: no fact split for this case'}
    passed, _, _ = grade_layer1(out, case_id)
    answer = prepare_answer(out, layer1_passed=passed)  # Removes the fixed partial sentence (design §5 M1).
    if answer is None:
        return {'pass': True, 'score': 1, 'reason': 'JUDGE_SKIPPED: not an answer that passed layer 1'}
    catalog, profiles = _reference()
    run_dir = Path(os.environ['SLICE7_RUN_DIR'])
    folder = run_dir / 'judge' / ('%s-a%s' % (case_id, variables['attempt']))
    ledger = Ledger(run_dir / 'ledger.json')
    case = judge_case(case_id, out, profiles[rows()[case_id]['__metadata:profile_id']], catalog)
    case['id'] = '%s#%s' % (case_id, variables['attempt'])  # Invalid-judge records name the attempt.
    verdict = judge_answer(judge_client(folder, ledger), ledger, case, answer)
    save_verdict(folder, verdict)  # Full claims/facts for the owner review (private, mode 600).
    if verdict['status'] != 'judged':
        return {'pass': False, 'score': 0, 'reason': verdict['reason']}  # JUDGE_INVALID: listed separately.
    reason = 'JUDGE_%s coverage %s' % ('PASS' if verdict['pass'] else 'FAIL', verdict['coverage'])
    return {'pass': verdict['pass'], 'score': 1 if verdict['pass'] else 0,
            'reason': reason + ''.join('; failed:' + name for name in verdict['failed_checks'])}


# Flow: provider output -> layer1: mapping v2 category vs dataset terminal, closed forbidden tables, generic read/
# cited/write/receipt/budget checks, route note for triage -> judge: skipped unless a judged case's answer passed
# layer 1 -> fixed text removed -> own + eligible cleared sources -> two terra calls -> code verdict saved.
