# Python 3.12 / unittest; Slice 7 LOCAL evaluation driver (design v3 §6, test 1)
# Protects: the evaluation never writes to Chroma Cloud. Knowledge is read-only from Cloud; profiles,
# conversations and escalations are local stand-ins per case.
import asyncio  # Drive the async client methods the app awaits.
import unittest

from routing import RoutingClient  # Module under test (evaluation/slice7/routing.py).


class FakeCloudCollection:
    """External Chroma boundary double: records every attribute call that reaches 'Cloud'."""

    def __init__(self):
        self.calls = []
        self.name, self.metadata = 'knowledge-cloud', {'embedding_model': 'text-embedding-3-small'}

    get_reply = {'ids': ['SU-HR-002']}
    query_reply = {'ids': [['SU-HR-002']]}

    async def get(self, **kwargs):
        self.calls.append(('get', kwargs))
        return self.get_reply

    async def query(self, **kwargs):
        self.calls.append(('query', kwargs))
        return self.query_reply

    async def add(self, **kwargs):  # A real Cloud collection has these; the wrapper must hide them.
        self.calls.append(('add', kwargs))

    async def upsert(self, **kwargs):
        self.calls.append(('upsert', kwargs))

    async def update(self, **kwargs):
        self.calls.append(('update', kwargs))

    async def delete(self, **kwargs):
        self.calls.append(('delete', kwargs))

    def modify(self, **kwargs):
        self.calls.append(('modify', kwargs))


class FakeCloudClient:
    def __init__(self):
        self.collection, self.requested = FakeCloudCollection(), []

    async def get_collection(self, name, *, embedding_function=None):
        self.requested.append(name)
        return self.collection


def routed():
    cloud = FakeCloudClient()
    local = {'profiles': object(), 'conversations': object(), 'escalations': object()}
    return cloud, local, RoutingClient(cloud, knowledge_name='su3-knowledge', local=local)


class RoutingClientTests(unittest.TestCase):
    def test_knowledge_write_methods_are_unreachable(self):
        cloud, _, client = routed()
        knowledge = asyncio.run(client.get_collection('su3-knowledge', embedding_function=None))
        for name in ('add', 'upsert', 'update', 'delete', 'modify'):
            with self.subTest(method=name), self.assertRaises(AttributeError):
                getattr(knowledge, name)
        self.assertEqual(cloud.collection.calls, [])  # Nothing reached Cloud.

    def test_knowledge_reads_pass_through_to_cloud(self):
        cloud, _, client = routed()
        knowledge = asyncio.run(client.get_collection('su3-knowledge', embedding_function=None))
        self.assertEqual(asyncio.run(knowledge.get(ids=['SU-HR-002'])), {'ids': ['SU-HR-002']})
        self.assertEqual(asyncio.run(knowledge.query(n_results=1)), {'ids': [['SU-HR-002']]})
        self.assertEqual(knowledge.metadata, {'embedding_model': 'text-embedding-3-small'})
        self.assertEqual([call[0] for call in cloud.collection.calls], ['get', 'query'])

    def test_knowledge_sources_returned_to_the_app_are_recorded(self):
        cloud, _, client = routed()
        cloud.collection.get_reply = {'ids': ['k1'], 'metadatas': [{'source_id': 'SU-HR-002'}]}
        cloud.collection.query_reply = {'ids': [['k2', 'k3']],
                                        'metadatas': [[{'source_id': 'SU-SD-001'}, {'source_id': 'SU-HR-010'}]]}
        knowledge = asyncio.run(client.get_collection('su3-knowledge', embedding_function=None))
        asyncio.run(knowledge.get(ids=['k1']))
        asyncio.run(knowledge.query(n_results=2))
        self.assertEqual(client.knowledge_reads, ['SU-HR-002', 'SU-SD-001', 'SU-HR-010'])

    def test_operational_collections_are_local_and_never_requested_from_cloud(self):
        cloud, local, client = routed()
        for name in ('profiles', 'conversations', 'escalations'):
            with self.subTest(name=name):
                self.assertIs(asyncio.run(client.get_collection(name, embedding_function=None)), local[name])
        self.assertEqual(cloud.requested, [])

    def test_unknown_collection_name_is_refused(self):
        cloud, _, client = routed()
        with self.assertRaises(KeyError):
            asyncio.run(client.get_collection('anything-else', embedding_function=None))
        self.assertEqual(cloud.requested, [])


if __name__ == '__main__':
    unittest.main()

# Summary: a fake Cloud client records every call; the routing client must expose only reads on
# Knowledge, serve operational names locally, and refuse unknown names, so no Cloud write is possible.
