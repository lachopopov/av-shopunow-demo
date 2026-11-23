# Python 3.12; explicit indexing gate/idempotency with controlled external operations.
import importlib
import unittest
from types import SimpleNamespace
from shopunow.knowledge import KnowledgeRecord
from tests.test_knowledge import source_row
import json


class KnowledgeIndexingTests(unittest.TestCase):
    def setUp(self):
        self.k=importlib.import_module('shopunow.knowledge')
        self.assertTrue(hasattr(self.k,'index_knowledge'),'Explicit verified Knowledge indexing is missing')
        self.row=KnowledgeRecord.model_validate_json(json.dumps(source_row()))
        self.calls=[]
        self.saved={}
        def read(key,*,timeout):
            self.calls.append('read')
            if key not in self.saved:
                return {'ids':[],'documents':[],'metadatas':[]}
            text,metadata=self.saved[key]
            return {'ids':[key],'documents':[text],'metadatas':[metadata]}
        def write(record,vector,*,timeout):
            self.calls.append('write')
            self.saved[record.source_id]=self.k.knowledge_payload(record)
        self.storage=SimpleNamespace(exact_knowledge=read,write_knowledge=write)
        def embed(texts,*,timeout):
            self.calls.append('embed')
            return [[1.0]+[0.0]*1535 for _ in texts]
        self.models=SimpleNamespace(embed=embed,embedding_model='text-embedding-3-small',dimensions=1536)

    def run_index(self,gate):
        return self.k.index_knowledge(catalog={self.row.source_id:self.row},storage=self.storage,
            models=self.models,account_check=gate,timeout=lambda:8)

    def test_account_gate_prevents_any_embedding_or_storage_and_exact_existing_is_reused(self):
        with self.assertRaises(ValueError):
            self.run_index(lambda:False)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.run_index(lambda:True),{'created':1,'existing':0})
        self.assertEqual(self.calls,['read','embed','write','read'])
        self.calls.clear()
        self.assertEqual(self.run_index(lambda:True),{'created':0,'existing':1})
        self.assertEqual(self.calls,['read'])

    def test_conflicting_source_never_overwritten_and_unknown_save_never_retried(self):
        self.saved[self.row.source_id]=('conflict',{})
        with self.assertRaises(ValueError):
            self.run_index(lambda:True)
        self.assertEqual(self.calls,['read'])
        self.saved.clear()
        self.calls.clear()
        def uncertain(*args,**kwargs):
            self.calls.append('write')
            raise TimeoutError('controlled failure')
        self.storage.write_knowledge=uncertain
        with self.assertRaises(TimeoutError):
            self.run_index(lambda:True)
        self.assertEqual(self.calls,['read','embed','write'])
