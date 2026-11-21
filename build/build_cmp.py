"""Turn the comparison-probe run (cmpprobe.json) into maps/cmp.json.

usage:  python build_cmp.py <cmpprobe.json> <tests.json> <out_dir> <importers.json>
Output: {"files": [...instrumented files...],
         "diff": {"file:line:col:op": [test numbers] | null}}   null = seen at import time
A site missing from "diff" never gave a different result under its partner operator.
"""
import json
import os
import sys
from collections import defaultdict

PROBE, TESTS_JSON, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
tests = json.load(open(TESTS_JSON))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file, by_prefix = defaultdict(list), defaultdict(list)
for t in tests:
    by_file[t["file"]].append(t["number"])
    by_prefix[t["id"].rsplit("::", 1)[0]].append(t["number"])


IMPORTERS = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}


def expand(ctx):
    if not ctx:
        return None
    kind, _, nodeid = ctx.partition("|")
    if kind == "I":
        return set(IMPORTERS[nodeid]) if nodeid in IMPORTERS else None
    if kind == "C":
        return {num[nodeid]} if nodeid in num else set()
    if kind in ("S", "T"):
        return set(by_prefix.get(nodeid.rsplit("::", 1)[0], [])) or set(by_file.get(nodeid.split("::")[0], []))
    if kind == "M":
        return set(by_file.get(nodeid, []))
    return None


p = json.load(open(PROBE))
diff = {}
for site, ctxs in p["diff"].items():
    sel = set()
    for c in ctxs:
        e = expand(c)
        if e is None:
            sel = None
            break
        sel |= e
    diff[site] = sorted(sel) if sel is not None else None
os.makedirs(OUT, exist_ok=True)
json.dump({"files": p["files"], "diff": diff}, open(os.path.join(OUT, "cmp.json"), "w"), separators=(",", ":"))
print("sites with a difference", len(diff), "import-time", sum(v is None for v in diff.values()),
      "bytes", os.path.getsize(os.path.join(OUT, "cmp.json")))
