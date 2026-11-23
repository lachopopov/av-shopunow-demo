# Pydantic 2.14.1 / Python 3.12; contact-free fallback writes under the caller's held lease.
from dataclasses import dataclass
from uuid import UUID
from shopunow.records import (HandoffOffer, PreclearanceTerminal,
                             verify_handoff_offer, verify_preclearance_offer_pair)
from shopunow.schemas import FallbackOutcome


@dataclass(frozen=True)
class PreclearanceIntent:
    offer: HandoffOffer  # No request text, model text, contacts or normal-message position.
    marker: PreclearanceTerminal


def record_preclearance_fallback(storage, *, attempt, tracking, user_id, conversation_id,
                                 request_id, offer_id, reason, created_at):
    """Called only within admitted owned conversation capacity; never release that capacity.

    The owner supplies fresh backend UUIDs and the captured identity. This routine does
    not admit requests, resolve uncertainty, retry writes or clear any prior exclusion.
    """
    failed = FallbackOutcome(kind='fallback_error',
                            text='The request could not be completed safely.', offer_id=None)
    common = dict(owner_user_id=user_id, conversation_id=str(UUID(conversation_id)),
                  request_id=str(UUID(request_id)), created_at=created_at, complete=True)
    offer = HandoffOffer.model_validate(dict(common, record_type='offer', offer_id=offer_id,
        reason_code=reason, target_department_or_fallback='human_support', offer_code='handoff_offer'))
    marker = PreclearanceTerminal.model_validate(dict(common, record_type='preclearance_terminal',
        marker_id='preclearance_terminal:' + UUID(request_id).hex, verified_offer_id=offer_id,
        reason_code=reason, marker_code='preclearance_terminal'))
    intent = PreclearanceIntent(offer, marker)
    started = False
    verified = False

    def timeout():
        # Each step uses the original admitted deadline; no new normal work after a stop.
        budget = attempt.normal_work_timeout()
        if budget is None:
            raise TimeoutError('Fallback operation is stopped.')
        return min(8.0, budget)  # Storage cap; models use the remaining question budget instead.

    try:
        budget = timeout()
        tracking.retain_preclearance(intent)
        budget = timeout()  # Retention may have waited; never reuse permission from before it.
        started = True  # Conservative before a write could reach the storage service.
        storage.write_offer(offer, timeout=budget)
        checked = verify_handoff_offer(storage.exact_offer(offer_id, timeout=timeout()),
            user_id=user_id, conversation_id=conversation_id, request_id=request_id,
            offer_id=offer_id, reason_code=reason, target_department_or_fallback='human_support')
        if checked.record != offer:
            raise ValueError('Intended fallback offer is unverified.')
        storage.write_preclearance_marker(marker, timeout=timeout())
        raw_marker = storage.exact_preclearance_marker(request_id, timeout=timeout())
        raw_offer = storage.exact_offer(offer_id, timeout=timeout())
        pair = verify_preclearance_offer_pair(raw_marker, raw_offer, user_id=user_id,
            conversation_id=conversation_id, request_id=request_id, offer_id=offer_id,
            reason_code=reason, target_department_or_fallback='human_support')
        if pair.record is None or pair.record.marker != marker or pair.record.offer != offer:
            raise ValueError('Intended fallback pair is unverified.')
        timeout()  # Late verification never permits display after withdrawal/deadline.
        verified = True
        return FallbackOutcome(kind='fallback_error',
                               text='The request could not be completed safely.', offer_id=offer_id)
    except Exception:
        return failed
    finally:
        if started and not verified:
            tracking.exclude_conversation(conversation_id)  # Also preserve holds on BaseException.


# Flow: retain intent -> offer/add/read -> marker/add/read -> linked offer re-read -> safe display.
