# Python 3.12 / chromadb 1.5.9 async client interface; Slice 7 LOCAL evaluation driver (design v3 §6)
# Routes the app's collection lookups: Knowledge -> Cloud, read-only; operational collections -> local stand-ins.


class ReadOnlyKnowledge:
    """Exposes only the reads the app uses (name, metadata, get, query); every other attribute is absent."""

    __slots__ = ('_collection', '_reads')  # No instance __dict__, so nothing else can be attached or looked up.

    def __init__(self, collection, reads):
        self._collection = collection  # The real Cloud collection; never handed out.
        self._reads = reads  # Shared list: source_id of every Knowledge record returned to the app.

    @property
    def name(self):
        return self._collection.name

    @property
    def metadata(self):
        return self._collection.metadata  # Read by the app's Knowledge configuration check.

    async def get(self, **kwargs):
        result = await self._collection.get(**kwargs)
        self._reads.extend(row.get('source_id') for row in result.get('metadatas') or [])
        return result

    async def query(self, **kwargs):
        result = await self._collection.query(**kwargs)
        self._reads.extend(row.get('source_id') for rows in result.get('metadatas') or [] for row in rows)
        return result


class RoutingClient:
    """Stands in for the Chroma client passed to the app's `connect`; only `get_collection` is offered."""

    def __init__(self, cloud_client, *, knowledge_name, local):
        self._cloud, self._knowledge_name, self._local = cloud_client, knowledge_name, dict(local)
        self.knowledge_reads = []  # For the case trace: forbidden-read checks (layer 1, design v3 §3 check 4).

    async def get_collection(self, name, *, embedding_function=None):
        if name == self._knowledge_name:  # The only name that may reach Cloud, wrapped read-only.
            collection = await self._cloud.get_collection(name, embedding_function=embedding_function)
            return ReadOnlyKnowledge(collection, self.knowledge_reads)
        return self._local[name]  # Operational names are local; an unknown name raises KeyError.


# Flow: app asks for a collection -> Knowledge name: Cloud collection behind a read-only wrapper;
# profiles/conversations/escalations: this case's local stand-ins; anything else: refused.
