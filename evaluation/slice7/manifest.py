# Python 3.12 stdlib; Slice 7 LOCAL evaluation — run manifest (design v3 §9 metadata header; challenger M3)
# Records what a run evaluated: commit X, sha256 digests of the dataset, Knowledge and evaluator tables, tool
# versions, mode and UTC start/end. Written privately (mode 600) into the run folder by run.sh.
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from cases import DATASET  # noqa: E402  The same dataset file the run reads (git-ignored, never published).
from wire import write_private_json  # noqa: E402

DIGESTED = {'cases.csv': DATASET,
            'knowledge.json': REPO / 'src' / 'shopunow' / 'assets' / 'knowledge.json',
            'facts.py': HERE / 'facts.py', 'two_turn.py': HERE / 'two_turn.py'}


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write(run_dir, phase, **extra):
    """phase 'start': commit, digests, rate date, versions and start time; phase 'end': end time."""
    path = Path(run_dir) / 'manifest.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    if phase == 'start':
        from wire import RATES
        commit = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                                check=True).stdout.strip()
        data.update(start_utc=_now(), commit=commit, rates_checked_on=RATES['checked_on'],
                    digests={name: hashlib.sha256(file.read_bytes()).hexdigest() for name, file in DIGESTED.items()},
                    **extra)
    else:
        data['end_utc'] = _now()
    write_private_json(path, data, indent=1)


if __name__ == '__main__':  # python manifest.py <run_dir> start|end [key=value ...]
    write(sys.argv[1], sys.argv[2], **dict(item.split('=', 1) for item in sys.argv[3:]))


# Flow: run.sh -> manifest start (commit, digests, versions) -> evaluation -> manifest end (time) -> report header.
