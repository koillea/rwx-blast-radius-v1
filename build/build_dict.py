"""Turn the dict-read probe run (dictprobe.json) into maps/dicts.bin.z.

usage:  python build_dict.py <dictprobe.json> <tests.json> <out_dir>
Output (marshal, zlib): {"sets": [[tests]...], "dicts": [{"owners", "name", "mro",
"get": {key repr: set index | -1}, "whole": set index | -1}]}; -1 = read outside any
test phase (not attributable: the selector falls back).
"""
import json
import marshal
import os
import sys
import zlib
from collections import defaultdict

PROBE, TESTS_JSON, OUT = sys.argv[1:4]
IMPORTERS = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
tests = json.load(open(TESTS_JSON))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file, by_prefix = defaultdict(list), defaultdict(list)
for t in tests:
    by_file[t["file"]].append(t["number"])
    by_prefix[t["id"].rsplit("::", 1)[0]].append(t["number"])


def expand(ctx):
    if not ctx:
        return None  # outside any test phase or import: not attributable
    kind, _, nodeid = ctx.partition("|")
    if kind == "I":  # read while a module was imported: every importer of it
        return set(IMPORTERS[nodeid]) if nodeid in IMPORTERS else None
    if kind == "C":
        return {num[nodeid]} if nodeid in num else set()
    if kind in ("S", "T"):
        return set(by_prefix.get(nodeid.rsplit("::", 1)[0], [])) or set(by_file.get(nodeid.split("::")[0], []))
    if kind == "M":
        return set(by_file.get(nodeid, []))
    return None


sets = {}


def I(ctxs):
    out = set()
    for c in ctxs:
        e = expand(c)
        if e is None:
            return -1
        out |= e
    return sets.setdefault(tuple(sorted(out)), len(sets))


raw = json.load(open(PROBE))
d, eager = raw["dicts"], raw["eager"]
dicts = []
for k in sorted(d, key=int):
    v = d[k]
    dicts.append({"owners": v["owners"], "name": v["name"], "mro": v["mro"], "keys": v.get("keys", []),
                  "get": {key: I(c) for key, c in v["get"].items()}, "whole": I(v["whole"])})
out = {"sets": [list(s) for s in sets], "dicts": dicts, "eager": eager}
os.makedirs(OUT, exist_ok=True)
open(os.path.join(OUT, "dicts.bin.z"), "wb").write(zlib.compress(marshal.dumps(out, 4), 9))
print("dicts", len(dicts), "distinct sets", len(sets), "bytes", os.path.getsize(os.path.join(OUT, "dicts.bin.z")))
