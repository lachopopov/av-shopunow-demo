# Python 3.12; Slice 7 LOCAL evaluation — one case's normalized trace for layer 1 (design v3 §3, §6)
# Built only from what the app actually did: recorded provider calls, Knowledge records returned to it,
# and the local stand-ins' calls/records. Named case_trace (not trace) to avoid shadowing the stdlib module.
import json

PRODUCE_PREFIX = 'Produce only the supplied authorized part'  # model_clients.AnswerModels.produce instructions.


def _payload(call):
    try:
        return json.loads(call['request'].get('input') or '{}')
    except (TypeError, ValueError):
        return {}


def _history_scan(call):
    """True for the app's owned-history scan: a get filtered on terminal-outcome kinds (sdk_writes.history_page)."""
    where = call.get('where') or {}
    return call['operation'] == 'get' and any('kind' in item for item in where.get('$and', []))


def build_trace(wire_calls, *, knowledge_reads, local, profiles_before, turns):
    producers = [call for call in wire_calls if call['endpoint'] == '/v1/responses'
                 and str(call['request'].get('instructions', '')).startswith(PRODUCE_PREFIX)]
    excerpts = {}  # excerpt_id -> Knowledge source_record_id, from the answer writer's own inputs.
    for call in producers:
        for row in _payload(call).get('evidence', []):
            if row.get('source_kind') == 'knowledge':
                excerpts[row['excerpt_id']] = row['source_record_id']
    outcome = ((turns[-1]['body'] or {}).get('outcome') or {}) if turns else {}  # Body is None on a raw 5xx.
    offers = {meta.get('offer_id'): meta.get('reason_code') for _, meta in local['escalations'].records.values()
              if meta.get('record_type') == 'offer'}
    return {
        'producer_parts': [_payload(call).get('part') for call in producers],
        'embedding_calls': sum(1 for call in wire_calls if call['endpoint'] == '/v1/embeddings'),
        'knowledge_reads': list(knowledge_reads),
        'history_reads': sum(1 for call in local['conversations'].calls if _history_scan(call)),
        'writes': [meta.get('record_type') for name in ('conversations', 'escalations')
                   for _, meta in local[name].records.values()],
        'profile_changed': dict(local['profiles'].records) != dict(profiles_before),
        'receipt': any('receipt_id' in (turn['body'] or {}) for turn in turns),
        'cited_sources': [excerpts.get(cited, 'unmapped:' + cited) for cited in outcome.get('cited_ids', [])],
        'cleared_sources': list(dict.fromkeys(excerpts.values())),  # Judge's approved text (design v3 §4 H2).
        'offer_reason': offers.get(outcome.get('offer_id')),
        'saved_receipts': [json.loads(document)['receipt_id'] for document, meta  # Read back (design §3a).
                           in local['escalations'].records.values() if meta.get('record_type') == 'submission'],
    }


# Flow: wire calls -> answer-writer parts, excerpt->source map, embedding count; stand-ins -> new record types,
# history scans, profile change, saved offer reason; last turn -> receipt and cited sources.
