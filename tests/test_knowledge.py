# Python 3.12; source integrity and access rules, with no external services.
import importlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


def source_row(**changes):
    row = dict(source_id='SU-SD-001', version='v1', source_question='What is tracking?',
        answer_facts=['Tracking describes synthetic delivery stages.'],
        eligible_audience=['customer'], subject='shipping_delivery', classification='general',
        access_scope='customer_general', approval_status='owner_approved',
        current_status='current', synthetic=True)
    return {**row, **changes}


def source_file(directory, rows):
    path = Path(directory)/'source.json'
    path.write_text(json.dumps(dict(schema_version='v1', dataset_status='owner_approved',
                                  synthetic=True, records=rows)))
    return path


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.knowledge'),
                             'Approved-source preparation and validation are missing')
        self.k = importlib.import_module('shopunow.knowledge')

    def prepare(self, directory, rows):
        target = Path(directory)/'assets'
        self.k.prepare_knowledge(source_file(directory, rows), target)
        return target, self.k.load_knowledge(target)

    def test_hit_is_checked_on_its_own_labels(self):
        # Pass 4 (owner 2026-11-24): Chroma is the only FAQ store; a hit is rebuilt and checked on its own labels.
        with TemporaryDirectory() as directory:
            _, catalog = self.prepare(directory, [source_row()])
            document, metadata = self.k.knowledge_payload(catalog['SU-SD-001'])
            self.assertEqual(document,'What is tracking?\nTracking describes synthetic delivery stages.')
            check = lambda key, text, labels: self.k.verify_knowledge(key, text, labels, role='customer',
                department=None, destination='shipping_delivery')
            self.assertEqual(check('SU-SD-001', document, metadata).source_id, 'SU-SD-001')
            for field, value in [('classification','non_general'), ('subject','hr'),
                                 ('eligible_audience',['employee']), ('source_id','SU-SD-002'),
                                 ('approval_status','draft'), ('synthetic',False), ('extra','label')]:
                with self.subTest(field=field):  # Inconsistent, foreign or unknown labels never pass.
                    self.assertIsNone(check('SU-SD-001', document, {**metadata,field:value}))
            self.assertIsNone(check('SU-SD-001', 'What is tracking?', metadata))  # A question without facts.
            # Accepted loss (owner 2026-11-24): an edited fact or version in Chroma is used as stored.
            self.assertEqual(check('SU-SD-001', document+' edited', {**metadata,'version':'v2'}).version, 'v2')

    def test_independent_index_and_source_tampering_fail_closed(self):
        with TemporaryDirectory() as directory:
            target, _ = self.prepare(directory, [source_row()])
            index = target/'knowledge_index.json'
            changed = json.loads(index.read_text())
            changed['entries']['SU-SD-001']['digest'] = '0'*64
            index.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                self.k.load_knowledge(target)

    def test_scope_filters_and_non_general_answer_boundary(self):
        rows = [source_row(), source_row(source_id='SU-HR-001', subject='hr',
            eligible_audience=['employee'], access_scope='employee_general'),
            source_row(source_id='SU-IT-010', subject='it_support', classification='non_general',
                eligible_audience=['employee'], access_scope='home_department_internal')]
        with TemporaryDirectory() as directory:
            _, catalog = self.prepare(directory, rows)
            self.assertEqual(self.k.source_disposition(catalog['SU-HR-001'],
                role='customer', department=None), 'restricted')
            self.assertEqual(self.k.source_disposition(catalog['SU-IT-010'],
                role='employee', department='it_support'), 'handoff')
            self.assertEqual(self.k.source_disposition(catalog['SU-IT-010'],
                role='employee', department='hr'), 'restricted')
            where = self.k.knowledge_filter(role='customer', destination='shipping_delivery')
            self.assertIn({'subject':'shipping_delivery'}, where['$and'])
            self.assertIn({'classification':'general'}, where['$and'])
            self.assertIn({'eligible_audience':{'$contains':'customer'}}, where['$and'])
            with self.assertRaises(ValueError):
                self.k.knowledge_filter(role='customer', destination='hr')

    def test_duplicate_unapproved_and_inconsistent_source_records_are_rejected(self):
        for rows in [[source_row(),source_row()], [source_row(current_status='obsolete')],
                     [source_row(synthetic=1)],
                     [source_row(eligible_audience=['customer'], subject='hr')]]:
            with self.subTest(rows=rows), TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    self.prepare(directory, rows)
