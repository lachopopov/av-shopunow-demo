# Python 3.12; Slice 7 LOCAL evaluation — local stand-ins for the three operational collections (design v3 §6)
# Adapted from the Slice 4 V4 driver's LocalCollection/matches (verified compatible with the current SDK records
# code by the design challenger). Not Cloud, not vector search: an in-memory store per case.


def matches(metadata, where):
    """The app's own Chroma selectors ($and/$or/$in/$ne/$eq/$contains) applied to one record's metadata."""
    if not where:
        return True
    if '$and' in where:
        return all(matches(metadata, item) for item in where['$and'])
    if '$or' in where:
        return any(matches(metadata, item) for item in where['$or'])
    for key, wanted in where.items():
        actual = metadata.get(key)
        if isinstance(wanted, dict):
            for operation, value in wanted.items():
                if operation == '$contains':
                    accepted = isinstance(actual, list) and value in actual
                elif operation == '$in':
                    accepted = actual in value
                elif operation == '$ne':
                    accepted = actual != value
                elif operation == '$eq':
                    accepted = actual == value
                else:
                    raise ValueError('Unsupported local SDK selector.')  # Never silently widen a filter.
                if not accepted:
                    return False
        elif actual != wanted:
            return False
    return True


class LocalCollection:
    """Immutable-record in-memory collection with the async add/get the SDK records code awaits."""

    def __init__(self, name):
        self.name, self.records, self.calls, self.metadata = name, {}, [], {}

    async def add(self, **payload):
        self.calls.append({'operation': 'add', 'ids': list(payload['ids'])})
        if len({len(payload[key]) for key in ('ids', 'documents', 'metadatas')}) != 1:
            raise ValueError('Mismatched native add arrays.')
        for key, document, metadata in zip(payload['ids'], payload['documents'], payload['metadatas'], strict=True):
            if key in self.records:
                raise ValueError('Immutable native record already exists.')  # The app never overwrites.
            self.records[key] = (document, dict(metadata))

    def raw(self, keys):
        keys = [key for key in keys if key in self.records]
        return {'ids': keys, 'documents': [self.records[key][0] for key in keys],
                'metadatas': [dict(self.records[key][1]) for key in keys],
                'embeddings': None, 'included': ['documents', 'metadatas']}

    async def get(self, *, include, ids=None, where=None, offset=0, limit=None):
        self.calls.append({'operation': 'get', 'ids': ids, 'where': where, 'offset': offset, 'limit': limit})
        keys = list(self.records) if ids is None else ids
        keys = [key for key in keys if key in self.records and matches(self.records[key][1], where)]
        keys = keys[offset:] if limit is None else keys[offset:offset + limit]
        return self.raw(keys)


async def seed_profiles(collection, profiles):
    """Provision the approved synthetic profiles exactly as SDKRecords.initialize_profile would store them."""
    for profile in profiles.values():
        await collection.add(ids=['profile:' + profile.user_id], documents=[profile.model_dump_json(warnings='error')],
                             metadatas=[{'record_type': 'profile', 'user_id': profile.user_id}],
                             embeddings=[[1.0, 0.0, 0.0]])


# Flow: per case, three fresh LocalCollections -> profiles seeded in the provisioner's record shape ->
# the app's own selectors run locally -> every call is kept in .calls for the trace.
