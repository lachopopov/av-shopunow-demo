# Python 3.12; reproducible packaging and real record validation, no storage/provider calls.
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class ProfileAssetsTests(unittest.TestCase):
    def test_packaged_roster_preserves_approved_bindings_and_rejects_drift(self):
        try:
            from shopunow import profiles
        except ImportError:
            self.fail('Reproducible packaged profile assets are missing.')
        roster = profiles.load_profiles()
        self.assertEqual(set(roster), {'syn-emp-hr-01','syn-emp-hr-02','syn-emp-it-01',
            'syn-emp-it-02','syn-emp-fac-01','syn-emp-fac-02','syn-cust-01','syn-cust-02','syn-cust-03'})
        self.assertEqual((roster['syn-emp-it-02'].role,roster['syn-emp-it-02'].home_department),
                         ('employee','it_support'))
        self.assertEqual((roster['syn-cust-03'].role,roster['syn-cust-03'].home_department),
                         ('customer',None))
        with TemporaryDirectory() as directory:
            source = Path(directory)/'profiles.json'
            source.write_bytes(profiles.asset_bytes('profiles.json'))
            left, right = Path(directory)/'left',Path(directory)/'right'
            for target in (left,right):
                profiles.prepare(source,target,created_at='2026-11-06T00:00:00Z')
            self.assertEqual((left/'profiles.json').read_bytes(),source.read_bytes())
            self.assertEqual((left/'provenance.json').read_bytes(),(right/'provenance.json').read_bytes())
            self.assertEqual(set(profiles.load_profiles(left)),set(roster))
            changed=json.loads((left/'profiles.json').read_text())
            changed['profiles'][0]['role']='customer'
            (left/'profiles.json').write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                profiles.load_profiles(left)
