#!/usr/bin/env bash
# Bash; Promptfoo 0.124.1 on Node 24.21.0; Slice 7 LOCAL evaluation run (design v3 §6, plan D10/D14)
# Usage: evaluation/slice7/run.sh smoke   -> free: stub app (no providers, no Cloud), 2 rows, plumbing check
#        SLICE7_CALIBRATION_APPROVED=yes evaluation/slice7/run.sh calibrate   -> PAID: 11 judge variants, 22 calls
#        SLICE7_PAID_APPROVED=yes SLICE7_CALIBRATION_RUN=<accepted calibration folder> evaluation/slice7/run.sh main
#            -> PAID: real app + judge + seam cases; calibration spend carried into the same $5 ledger (design §7)
# Paid modes refuse uncommitted changes in src/, evaluation/ or ui/, so the run matches commit X (manifest.json).
# Everything (config, tests, results, Promptfoo state/cache, ledger, wire logs) goes to a new mode-700 run folder.
set -euo pipefail
MODE=${1:?usage: run.sh smoke|calibrate|main}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
HERE=$REPO/evaluation/slice7
PYTHON=$REPO/.venv/bin/python
PROMPTFOO=$REPO/node_modules/.bin/promptfoo  # Pinned 0.124.1 in package.json (npm ci); node_modules is git-ignored.
NODE_BIN=$HOME/.nvm/versions/node/v24.21.0/bin
[ -x "$NODE_BIN/node" ] || { echo 'Node 24.21.0 not found (nvm); refusing to fall back to the shell Node' >&2; exit 2; }
export PATH=$NODE_BIN:$PATH   # Default shell Node is v20.

case $MODE in
  smoke) BUILDER=test_case_runner:stub_build ;;
  main) [ "${SLICE7_PAID_APPROVED:-}" = yes ] || { echo 'main run needs owner approval: SLICE7_PAID_APPROVED=yes' >&2; exit 2; }
        [ -f "${SLICE7_CALIBRATION_RUN:-}/ledger.json" ] || { echo 'main needs SLICE7_CALIBRATION_RUN=<calibration folder>' >&2; exit 2; }
        SLICE7_CALIBRATION_RUN=$(cd "$SLICE7_CALIBRATION_RUN" && pwd)  # Absolute: the script changes folder later.
        BUILDER=app_builder:real_build ;;
  calibrate) [ "${SLICE7_CALIBRATION_APPROVED:-}" = yes ] || { echo 'calibration needs owner approval: SLICE7_CALIBRATION_APPROVED=yes' >&2; exit 2; } ;;
  *) echo 'usage: run.sh smoke|calibrate|main' >&2; exit 2 ;;
esac

if [ "$MODE" != smoke ] && [ -n "$(git -C "$REPO" status --porcelain -- src evaluation ui tests pyproject.toml uv.lock)" ]; then
  echo 'Uncommitted changes in src/, evaluation/, ui/, tests/ or the lockfiles: commit first so the run matches commit X' >&2; exit 2
fi

umask 077  # Every file and folder created below is private to the owner.
RUN=$REPO/evaluation/results/slice7-$MODE-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$RUN" && chmod 700 "$RUN"
git -C "$REPO" rev-parse HEAD > "$RUN/commit.txt"  # Evaluated commit X for the report header.
git -C "$REPO" status --porcelain > "$RUN/worktree-status.txt"
PF_PACKAGE=$REPO/node_modules/promptfoo/package.json
PF_VERSION=$(sed -n 's/^  "version": "\(.*\)",$/\1/p' "$PF_PACKAGE")
"$PYTHON" "$HERE/manifest.py" "$RUN" start mode="$MODE" node="$("$NODE_BIN/node" --version)" \
  promptfoo="$PF_VERSION" calibration_run="${SLICE7_CALIBRATION_RUN:-}"  # Design §9 metadata header.

if [ "$MODE" = calibrate ]; then  # Judge only, no app: design v3 §5. Same ledger rules ($5 stop, judge cap).
  cd "$HERE"
  SLICE7_RUN_DIR=$RUN uv run --frozen --project "$REPO" --env-file "$REPO/.env" -- "$PYTHON" "$HERE/calibration.py" "$RUN"
  "$PYTHON" -c "import sys; sys.path.insert(0,'$HERE'); from ledger import Ledger; \
print(Ledger('$RUN/ledger.json').totals())"
  "$PYTHON" "$HERE/manifest.py" "$RUN" end
  echo "run folder: $RUN"
  exit 0
fi

# Promptfoo state, cache, telemetry and sharing stay inside the run folder or off (plan D14).
export SLICE7_RUN_DIR=$RUN PROMPTFOO_PYTHON=$PYTHON PROMPTFOO_ASSERTIONS_MAX_CONCURRENCY=1 \
  PROMPTFOO_CONFIG_DIR=$RUN/promptfoo-state PROMPTFOO_CACHE_PATH=$RUN/promptfoo-cache PROMPTFOO_CACHE_ENABLED=false \
  PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 PROMPTFOO_DISABLE_SHARING=1 \
  PROMPTFOO_DISABLE_REMOTE_GENERATION=true

if [ "$MODE" = smoke ]; then  # Two rows; the stub app always refuses, so layer 1 must report a wrong path.
  "$PYTHON" -c "import json,sys; sys.path.insert(0,'$HERE'); from cases import runs; \
rows=[r for r in runs() if (r['vars']['case_id'],r['vars']['attempt']) in {('FACT-008',1),('RULE-003',1)}]; \
open('$RUN/tests.json','w').write(json.dumps(rows))"
else
  "$PYTHON" "$HERE/cases.py" "$RUN/tests.json"
fi

cat > "$RUN/promptfooconfig.yaml" <<EOF
description: Slice 7 LOCAL evaluation ($MODE)
prompts:
  - '{{query}}'
providers:
  - id: file://$HERE/provider.py:call_api
    label: shopunow-slice7-$MODE
    config:
      builder: $BUILDER
      timeout: 900000  # ms; above the provider's 600-s case subprocess timeout.
      pythonExecutable: $PYTHON
tests: file://$RUN/tests.json
defaultTest:
  assert:
    - type: python
      value: file://$HERE/assertions.py:layer1
    - type: python
      value: file://$HERE/assertions.py:judge
evaluateOptions:
  maxConcurrency: 1
sharing: false
EOF

cd "$HERE"  # Provider/assertion imports resolve from here; the app package comes from the venv.
if [ "$MODE" = smoke ]; then  # No credentials: the stub app has no provider or Cloud clients.
  SHOPUNOW_API_TOKEN=synthetic-eval-token "$PROMPTFOO" eval -c "$RUN/promptfooconfig.yaml" --no-cache \
    --no-share -j 1 -o "$RUN/results.json" || true
else  # Credentials from .env into this process tree only; never printed.
  # Calibration spend and judge calls count against the same $5 stop and 90-call cap (design §7).
  "$PYTHON" -c "import sys; sys.path.insert(0,'$HERE'); from ledger import Ledger; \
Ledger('$RUN/ledger.json').carry_over('$SLICE7_CALIBRATION_RUN/ledger.json')"
  uv run --frozen --project "$REPO" --env-file "$REPO/.env" -- "$PROMPTFOO" eval -c "$RUN/promptfooconfig.yaml" \
    --no-cache --no-share -j 1 -o "$RUN/results.json" || true
  # Seam cases S4/P4 (labelled "seam", reported separately; same ledger and $5 stop).
  uv run --frozen --project "$REPO" --env-file "$REPO/.env" -- "$PYTHON" "$HERE/seam.py" "$RUN" || true
  "$PYTHON" -c "import sys; sys.path.insert(0,'$HERE'); from ledger import Ledger; \
print(Ledger('$RUN/ledger.json').totals())"  # Totals (incl. run_invalid) come from the ledger, not Promptfoo.
fi
"$PYTHON" "$HERE/manifest.py" "$RUN" end
echo "run folder: $RUN"

# Flow: mode gate (main needs explicit approval) -> private run folder + commit -> tests.json from the approved
# case list -> generated Promptfoo config (provider + layer1/judge assertions) -> promptfoo eval -> seam cases
# (main only) -> ledger totals.
