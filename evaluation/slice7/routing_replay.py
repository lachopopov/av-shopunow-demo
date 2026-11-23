# Python 3.12; OpenAI SDK 3.46 + httpx2 2.16.0 via the Slice 7 recording transport (wire.py).
# Routing tie-breaker replay (owner request 2026-11-23): send the 26 RoutingResolution requests recorded in the
# Pass 2 main run, unchanged except model/effort, to Sol medium (baseline), Luna medium and Luna high.
# Saves every reply, times every call and prices it into a private ledger with a $1.00 hard stop. No app code,
# no Cloud, no app state. Usage:
#   uv run python evaluation/slice7/routing_replay.py --dry-run          (free: canned replies, plumbing check)
#   ROUTING_REPLAY_APPROVED=yes uv run python evaluation/slice7/routing_replay.py   (PAID, about $0.45)
#   uv run python evaluation/slice7/routing_replay.py --analyse <run folder>        (free: compare the replies)
import argparse  # Command-line modes.
import copy  # Each variant gets its own copy of the recorded body.
import glob  # Find the recorded wire files.
import json  # Request/reply files.
import os  # Environment flag, private folders.
import statistics  # Medians for timing.
import sys  # Exit codes.
from datetime import datetime, timezone  # Run folder name.
from decimal import Decimal  # Ledger amounts.
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import httpx2  # noqa: E402 - only for the dry-run mock transport.
from ledger import Ledger  # noqa: E402 - same private ledger as the evaluation runs.
from wire import WireLog, recording_openai, write_private_json  # noqa: E402

REPO = HERE.parents[1]
SOURCE = REPO / 'evaluation' / 'results' / 'slice7-main-20261122T140941Z'  # Pass 2 main run (evaluated X).
VARIANTS = (('sol-medium', 'gpt-6.1-sol', 'medium'),  # Baseline: today's tie-breaker.
            ('luna-medium', 'gpt-6-luna', 'medium'),
            ('luna-high', 'gpt-6-luna', 'high'),
            # Owner request 2026-11-23: Sol at its lowest accepted effort (probe with --only/--limit first).
            ('sol-none', 'gpt-6.1-sol', 'none'),
            ('sol-minimal', 'gpt-6.1-sol', 'minimal'),
            ('sol-low', 'gpt-6.1-sol', 'low'))
STOP = Decimal('1.00')  # Owner-approved budget for this replay; the expected spend is about $0.45.
RESERVE = Decimal('0.05')  # A call starts only if spent + this stays within the stop (Sol median $0.015).
# Must-pass (pre-registered 2026-11-23): the correct entry Jev rejected and the Sol tie-breaker rescued.
MUST_PASS = {'FACT-006-a1': 'SU-IT-003', 'FACT-010-a1': 'SU-BP-002', 'FACT-014-a1': 'SU-SD-006',
             'FACT-019-a1': 'SU-SD-003'}


def recorded_requests():
    """The 26 recorded tie-breaker requests: (case, body), in case order."""
    found = []
    for case in sorted(glob.glob(str(SOURCE / 'cases' / '*'))):
        for path in sorted(glob.glob(case + '/wire/wire-*.json')):
            call = json.load(open(path, encoding='utf-8'))
            if ((call['request'].get('text') or {}).get('format') or {}).get('name') == 'RoutingResolution':
                found.append((Path(case).name, call['request']))
    return found


def api_key():
    """Read ONE named .env line inside this process; the value is never printed or saved."""
    for line in (REPO / '.env').read_text(encoding='utf-8').splitlines():
        if line.startswith('OPENAI_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise SystemExit('OPENAI_API_KEY line missing in .env')


def canned(request):
    """Dry-run reply in the Responses API shape, with valid usage so pricing works."""
    body = json.loads(request.content)
    payload = json.loads(body['input'])
    reply = {'question': payload['question'], 'status': 'resolved', 'intent': None, 'dependency': None,
             'destinations': [], 'source_matches': []}
    return httpx2.Response(200, json={
        'id': 'resp_dry', 'object': 'response', 'model': body['model'], 'status': 'completed',
        'output': [{'type': 'message', 'id': 'msg_dry', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': json.dumps(reply), 'annotations': []}]}],
        'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15,
                  'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                  'output_tokens_details': {'reasoning_tokens': 0}}})


def run(dry, only=None, limit=None):
    folder = REPO / 'evaluation' / 'results' / ('slice7-routing-replay-' + ('dry-' if dry else '') +
                                                datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    os.makedirs(folder, mode=0o700)
    ledger = Ledger(folder / 'ledger.json', stop=STOP)
    key = 'dry-run-key' if dry else api_key()
    inner = httpx2.MockTransport(canned) if dry else None
    replies = []
    chosen = [v for v in VARIANTS if only is None or v[0] in only]  # Default: every variant.
    for case, recorded in recorded_requests()[:limit]:  # limit=None: all 26 requests.
        for label, model, effort in chosen:  # Interleaved per request: time-of-day effects hit all variants.
            totals = ledger.totals()
            if totals['unknown_items'] or totals['known_usd'] + RESERVE > STOP:
                print('stop: budget reached or unknown usage; remaining calls not sent')
                write_private_json(folder / 'replies.json', replies, indent=1)
                return folder
            log = WireLog(folder / label / case, ledger=ledger, kind=label)
            body = copy.deepcopy(recorded)
            body['model'], body['reasoning'] = model, {'effort': effort}
            entry = {'case': case, 'variant': label, 'model': model, 'effort': effort}
            try:
                with recording_openai(log, api_key=key, inner=inner) as client:
                    response = client.responses.create(**body)
                entry['reply'] = json.loads(response.output_text)
            except Exception as error:  # One attempt only; the type is kept, never the message.
                entry['error'] = type(error).__name__
            entry['elapsed_s'] = log.calls[-1]['elapsed_ms'] / 1000 if log.calls else None
            entry['usd'] = log.calls[-1]['usd'] if log.calls else None
            replies.append(entry)
            print(case, label, entry.get('error') or 'ok', entry['elapsed_s'] and f"{entry['elapsed_s']:.1f}s")
    write_private_json(folder / 'replies.json', replies, indent=1)
    print('spent', ledger.totals()['known_usd'], 'USD; folder', folder)
    return folder


def analyse(folder):
    """Compare each Luna variant with the Sol replay and the pre-registered criteria (free)."""
    replies = json.load(open(Path(folder) / 'replies.json', encoding='utf-8'))
    by = {(r['case'], r['variant']): r for r in replies}
    cases = sorted({r['case'] for r in replies})
    for label, _, _ in VARIANTS:
        rows = [by.get((case, label)) for case in cases]
        if not any(rows):
            continue  # Variant not part of this run.
        ok = [r for r in rows if r and 'reply' in r]
        times = [r['elapsed_s'] for r in ok]
        print(f'== {label}: {len(ok)}/{len(cases)} valid replies; median {statistics.median(times):.1f}s, '
              f'max {max(times):.1f}s; spent ${sum(Decimal(r["usd"]) for r in ok if r["usd"]):.4f}')
        rescued = []
        for case, source in MUST_PASS.items():
            reply = (by.get((case, label)) or {}).get('reply') or {}
            match = next((m['match'] for m in reply.get('source_matches', []) if m['source_id'] == source), None)
            rescued.append(f'{case}:{source}={match}')
        print('   must-pass:', ', '.join(rescued))
        if label == 'sol-medium':
            continue
        same, lost = 0, []
        for case in cases:
            base, test = (by.get((case, 'sol-medium')) or {}).get('reply'), (by.get((case, label)) or {}).get('reply')
            if not base or not test:
                continue
            if base['status'] == test['status'] and set(base['destinations']) == set(test['destinations']):
                same += 1
            missing = set(base['destinations']) - set(test['destinations'])
            if missing:
                lost.append(f'{case}:{sorted(missing)}')
        print(f'   status+destinations equal to Sol: {same}/{len(cases)}; departments lost vs Sol: {lost or "none"}')


def main():
    parser = argparse.ArgumentParser(description='Routing tie-breaker replay: Sol vs Luna medium/high.')
    parser.add_argument('--dry-run', action='store_true', help='free: canned replies, no provider call')
    parser.add_argument('--analyse', metavar='FOLDER', help='free: compare saved replies')
    parser.add_argument('--only', help='comma-separated variant labels, e.g. sol-medium,sol-low')
    parser.add_argument('--limit', type=int, help='first N recorded requests only (probe)')
    options = parser.parse_args()
    if options.analyse:
        return analyse(options.analyse)
    if not options.dry_run and os.environ.get('ROUTING_REPLAY_APPROVED') != 'yes':
        raise SystemExit('paid replay needs owner approval: ROUTING_REPLAY_APPROVED=yes')
    print(len(recorded_requests()), 'recorded routing requests')
    run(options.dry_run, only=options.only.split(',') if options.only else None, limit=options.limit)


if __name__ == '__main__':
    main()

# Flow: recorded RoutingResolution bodies -> per request, Sol / Luna medium / Luna high (model+effort only
# changed) -> recording transport (allowlist, timing, pricing into a $1.00 ledger) -> replies.json ->
# --analyse: timing, must-pass rescues, agreement with Sol, lost departments.
