"""Turn the flag-probe run (attrprobe.json) into maps/attrs.bin.z.

usage:  python build_attr.py <attrprobe.json> <tests.json> <out_dir> <importers.json>
Output (marshal, zlib): {"sets", "reads": {"owner|NAME": set index | -1},
"pass": {attr: idx | -1}, "passkw": {kw: attr}, "files": [...], "unknown": [NAME...]}
-1 = read outside any test phase or import (not attributable): the selector falls back.
"""
import json
import marshal
import os
import sys
import zlib
from collections import defaultdict

PROBE, TESTS_JSON, OUT, IMP = sys.argv[1:5]
IMPORTERS = json.load(open(IMP))
tests = json.load(open(TESTS_JSON))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file, by_prefix = defaultdict(list), defaultdict(list)
for t in tests:
    by_file[t["file"]].append(t["number"])
    by_prefix[t["id"].rsplit("::", 1)[0]].append(t["number"])


import marshal as _marshal
import zlib as _zlib

# State built while module Y is imported (class bodies, metaclasses, tries...) is seen by
# Y's importers AND by every test that uses Y: runs its code or creates its classes,
# e.g. a test reaching a dialect through the registry without importing it.
_MAPS = _marshal.loads(_zlib.decompress(open(os.path.join(OUT, "maps.bin.z"), "rb").read()))
_MSETS = _MAPS["sets"]


def users_of(mod):
    rel = mod.replace(".", "/")
    out = set()
    for f in (rel + ".py", rel + "/__init__.py"):
        for key in ("file_cov", "file_use"):
            i = _MAPS[key].get(f)
            if i is not None:
                out |= set(_MSETS[i])
    return out


def expand(ctx):
    if not ctx:
        return None
    kind, _, nodeid = ctx.partition("|")
    if kind == "I":  # read while a module was imported: its importers and its users
        return (set(IMPORTERS[nodeid]) | users_of(nodeid)) if nodeid in IMPORTERS else None
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


p = json.load(open(PROBE))
reads = {f"{o}|{n}": I(c) for o, n, c in p["reads"] if not o.startswith("?")}
unknown = sorted({n for o, n, c in p["reads"] if o.startswith("?")})
passes = {a: I(c) for a, c in p["pass"].items()}  # before the sets are written out
out = {"sets": [list(s) for s in sets], "reads": reads, "pass": passes,
       "passkw": p["passkw"], "files": p["files"], "unknown": unknown}
open(os.path.join(OUT, "attrs.bin.z"), "wb").write(zlib.compress(marshal.dumps(out, 4), 9))
print("reads", len(reads), "pass", len(out["pass"]), "sets", len(sets), "unattributed owners", len(unknown),
      "bytes", os.path.getsize(os.path.join(OUT, "attrs.bin.z")))
