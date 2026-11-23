# Chroma v1.5.9 / Python 3.12; injected native reads, never client construction.
from functools import partial  # Bind public SDK operations to the existing worker bridge.
from time import monotonic  # Include bridge waits and all page processing in one budget.
from uuid import UUID  # Reuse the established UUID-hex storage keys.


class SDKReads:
    """Thin native-get mappings; existing record verifiers retain content authority."""

    def __init__(self, worker, *, profiles, conversations, escalations):
        self._worker = worker  # The caller owns the accepted client/loop lifetime.
        self._profiles = profiles  # Prepared profiles and preferences share a collection.
        self._conversations = conversations  # Headers and transcript events share a collection.
        self._escalations = escalations  # Contact-free historical offer records.

    def _get(self, collection, *, timeout, **selectors):
        operation = partial(collection.get, include=['metadatas', 'documents'], **selectors)
        return self._worker.call(operation, timeout=timeout)

    def profile(self, user_id, *, timeout):
        return self._get(self._profiles, ids=['profile:' + user_id], timeout=timeout)

    def header(self, conversation_id, *, timeout):
        return self._get(self._conversations,
                         ids=['conversation_header:' + UUID(conversation_id).hex], timeout=timeout)

    def preference(self, user_id, *, timeout):
        return self._get(self._profiles,
                         ids=['preference:' + user_id + ':response_style'], timeout=timeout)

    def exact_offer(self, offer_id, *, timeout):
        return self._get(self._escalations, ids=['offer:' + UUID(offer_id).hex], timeout=timeout)

    def headers(self, user_id, *, timeout):
        return self._scan(timeout=timeout, limit=None,
                         where={'$and': [{'record_type': 'conversation_header'},
                                         {'owner_user_id': user_id}]})

    def events(self, conversation_id, *, limit, timeout):
        if type(limit) is not int or limit < 1:
            raise ValueError('Invalid event read limit.')
        return self._scan(timeout=timeout, limit=limit,
                         where={'$and': [{'conversation_id': str(UUID(conversation_id))},
                                         {'record_type': {'$ne': 'conversation_header'}}]})

    def _scan(self, *, where, limit, timeout):
        deadline = monotonic() + min(8.0, timeout)  # One logical read, not a budget per page.

        def remaining():
            budget = deadline - monotonic()
            if budget <= 0:
                raise TimeoutError('The SDK read budget expired.')
            return budget

        async def scan():
            offset, result = 0, None
            while True:
                remaining()  # Recheck after bridge scheduling and before each native call.
                page_limit = 100 if limit is None else min(100, limit - offset)
                page = await self._conversations.get(where=where, offset=offset,
                    limit=page_limit, include=['metadatas', 'documents'])
                remaining()  # Synchronous SDK response processing consumes the same budget.
                if not isinstance(page, dict) or any(
                    not isinstance(page.get(key), list) for key in ('ids', 'metadatas', 'documents')
                ):
                    raise ValueError('Invalid SDK read page.')
                count = len(page['ids'])
                if count > page_limit or len(page['documents']) != count or len(page['metadatas']) != count:
                    raise ValueError('Invalid SDK read page.')
                if result is None:
                    result = {**page, 'ids': [], 'metadatas': [], 'documents': []}
                for key in ('ids', 'metadatas', 'documents'):
                    result[key].extend(page[key])  # Preserve suspicious rows and duplicate IDs.
                offset += count  # Advance by actual rows, including short pages.
                remaining()  # Never return success after aggregation has spent the budget.
                if count == 0 or offset == limit:
                    return result  # Empty page proves termination only for this supplied scan.

        return self._worker.call(scan, timeout=remaining())  # Existing bridge enforces cancellation.


# Flow: existing coordinator -> native exact/get selector -> original worker deadline
# -> untrusted native result -> existing ownership/transcript verifier.
