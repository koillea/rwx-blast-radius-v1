#!/usr/bin/env bash
# Rebuild maps/ from the pinned sqlglot commit. Run once, offline from scoring;
# the result is committed so the Docker build needs no network and takes seconds.
#
#   bash build/rebuild_maps.sh /path/to/tests.json
#
# Everything comes from sqlglot at the pinned commit: per-test coverage, the sqlglot
# classes each test instantiates, and the fixture files each test opens. Nothing is
# derived from any patch.
set -euo pipefail
PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
HERE="$(cd "$(dirname "$0")" && pwd)"
TESTS_JSON="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
WORK="${WORK:-$(mktemp -d)}"

cd "$WORK"
[ -d sqlglot ] || git clone -q https://github.com/tobymao/sqlglot
cd sqlglot && git checkout -q "$PIN"
python3 -m venv ../venv && . ../venv/bin/activate
pip install -q -e ".[dev]" coverage pytest

rm -f .coverage usage.json
PYTHONPATH="$HERE" MAPPLUGIN_OUT="$PWD/usage.json" \
  coverage run --rcfile="$HERE/coveragerc" -m pytest tests/ -p mapplugin -p no:cacheprovider -q
python3 "$HERE/build_maps.py" . "$TESTS_JSON" "$HERE/../maps"

# Second pass (no coverage): which tests ever evaluate each comparison on operands
# where its partner operator (is/==, is not/!=, </<=, >/>=) would disagree.
rm -f cmpprobe.json
PYTHONPATH="$HERE" CMPPROBE_OUT="$PWD/cmpprobe.json" MAPPLUGIN_OUT=/dev/null \
  python3 -m pytest tests/ -p cmpprobe -p mapplugin -p no:cacheprovider -q
python3 "$HERE/build_cmp.py" cmpprobe.json "$TESTS_JSON" "$HERE/../maps" importers.json
