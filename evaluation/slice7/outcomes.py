# Python 3.12; Slice 7 LOCAL evaluation — mapping table v2 (design v3 §3)
# Turns one wire observation into a PATH category and one dataset terminal/route into the expected category.
# Versioned: changing either table is a design change (record it in the design file).
from shopunow.workflow import PARTIAL_SENTENCE  # The app's own CD-098 text; never a drifting copy.

MAPPING_VERSION = 'v2.1-2026-11-22'  # v2.1: owner decision after the main run (reported with the v2 result too).

OFFER_REASONS = {  # Saved offer reason_code -> path category (workflow.py offer(), preclearance.py).
    'insufficient_evidence': 'offer:insufficient_evidence',
    'handoff_needed': 'offer:handoff_needed',
    'security_uncertain': 'security_fallback',
    'technical_failure': 'technical',
    'revision_rounds_exhausted': 'technical',
    'dependency_failure': 'technical',
}
TECHNICAL_ERRORS = {'technical_error', 'request_timeout', 'request_stopped'}  # api.QUESTION_ERRORS endings.
SETUP_ERRORS = {'execution_capacity', 'interaction_not_ready', 'conversation_read_only', 'conversation_busy',
                'conversation_unresolved', 'conversation_full', 'profile_unavailable', 'question_limit'}
PLAIN_KINDS = {'confirmed_attack_block': 'attack_block', 'refusal': 'refusal', 'clarification': 'clarification'}


def classify(observation):
    """Wire observation (status, kind/error code, text, offer id, saved reason) -> path category."""
    if observation.get('provider_errors'):
        return 'invalid'  # A provider HTTP error is an invalid run, not an app result (evaluation plan §3).
    if observation.get('http_status') != 200:
        code = observation.get('error_code')
        if code in SETUP_ERRORS:
            return 'invalid'  # Checked first: some setup errors are 503s (execution_capacity).
        if code in TECHNICAL_ERRORS or (observation.get('http_status') or 0) >= 500:
            return 'technical'  # Mapping v2: any other HTTP 5xx is a technical ending (challenger M4).
        return {'invalid_request': 'validation_error', 'ownership_failure': 'ownership_error'}.get(code, 'unknown')
    kind = observation.get('kind')
    if kind == 'answer':
        return 'partial_answer' if PARTIAL_SENTENCE in observation.get('text', '') else 'answer'
    if kind in ('handoff_offer', 'fallback_error'):
        if observation.get('offer_id') is None:
            return 'technical'  # An unverified fallback: nothing was saved.
        return OFFER_REASONS.get(observation.get('reason_code'), 'unknown')
    return PLAIN_KINDS.get(kind, 'unknown')


TERMINALS = {  # Dataset expected_terminal -> category (handoff_offer depends on the route, below).
    'reviewed_answer': 'answer', 'partial_result': 'partial_answer', 'attack_block': 'attack_block',
    'refusal': 'refusal', 'clarification': 'clarification', 'technical_error': 'technical',
    'validation_error': 'validation_error', 'ownership_error': 'ownership_error', 'verified_receipt': 'receipt',
}
OFFER_ROUTES = {'insufficient_knowledge': 'offer:insufficient_evidence', 'security_fallback': 'security_fallback'}


ACCEPTED = {'answer': {'answer', 'partial_answer'}}  # v2.1: the CD-098 support sentence alone is good practice.


def accepts(wanted, category):
    """Does the observed category satisfy the expected one? (v2.1; every other category must match exactly.)"""
    return category in ACCEPTED.get(wanted, {wanted})


def expected(terminal, route):
    """Dataset expected_terminal + expected_route -> expected path category; unknown terminal raises KeyError."""
    if terminal == 'handoff_offer':
        return OFFER_ROUTES.get(route, 'offer:handoff_needed')  # A department route means handoff_needed.
    return TERMINALS[terminal]


# Flow: observation -> invalid (provider/setup) | technical | validation/ownership | answer vs partial |
# offer by saved reason | plain kind; dataset row -> expected category; layer 1 compares the two.
