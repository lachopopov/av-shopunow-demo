# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §9 metadata header; challenger M3)
# Protects: each run records what was evaluated (commit X, digests of the dataset, Knowledge and evaluator tables,
# tool versions, UTC start/end), so the report can prove the run matches commit X and the PUBLIC snapshot.
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import manifest  # Module under test (evaluation/slice7/manifest.py).

REPO = Path(__file__).resolve().parents[2]


class ManifestTests(unittest.TestCase):
    def test_start_and_end_record_commit_digests_versions_and_times(self):
        run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest.write(run_dir, 'start', mode='main', node='v24.21.0', promptfoo='0.124.1')
        manifest.write(run_dir, 'end')
        data = json.loads((run_dir / 'manifest.json').read_text())
        head = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              check=True).stdout.strip()
        self.assertEqual(data['commit'], head)
        dataset = REPO / 'datasets' / 'cases.csv'
        self.assertEqual(data['digests']['cases.csv'], hashlib.sha256(dataset.read_bytes()).hexdigest())
        self.assertEqual(set(data['digests']), {'cases.csv', 'knowledge.json', 'facts.py', 'two_turn.py'})
        self.assertEqual((data['mode'], data['node'], data['promptfoo']), ('main', 'v24.21.0', '0.124.1'))
        self.assertTrue(data['start_utc'] < data['end_utc'] or data['start_utc'] == data['end_utc'])
        self.assertEqual(data['rates_checked_on'], '2026-11-24')
        self.assertEqual(os.stat(run_dir / 'manifest.json').st_mode & 0o077, 0)


if __name__ == '__main__':
    unittest.main()

# Summary: start/end manifest entries carry commit X, sha256 digests of cases.csv, knowledge.json and the evaluator
# tables, tool versions, mode and UTC times, in a private file.
