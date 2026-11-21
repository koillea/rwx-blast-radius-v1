"""Turn the map-build run (coverage DB + mapplugin usage.json) into the compact maps
the selector ships with. Everything here is derived from the pinned sqlglot commit.

usage:  python build_maps.py <sqlglot_checkout> <tests.json> <out_dir>
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
from collections import defaultdict

from coverage.numbits import numbits_to_nums

ROOT, TESTS_JSON, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(OUT, exist_ok=True)

tests = json.load(open(TESTS_JSON))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file: dict[str, list[int]] = defaultdict(list)
by_prefix: dict[str, list[int]] = defaultdict(list)
for t in tests:
    by_file[t["file"]].append(t["number"])
    by_prefix[t["id"].rsplit("::", 1)[0]].append(t["number"])


usage = json.load(open(os.path.join(ROOT, "usage.json")))
EDGES = {k: set(v) for k, v in usage.get("edges", {}).items()}
RUNTIME = usage.get("runtime_imports", {})
MODFILE = usage.get("modfile", {})


def _with_parents(name):
    parts = name.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


_closure_cache: dict[str, set[str]] = {}


def closure(mod: str) -> set[str]:
    """Every module importing `mod` imports (itself and its parent packages included)."""
    if mod in _closure_cache:
        return _closure_cache[mod]
    seen, todo = set(), [mod]
    while todo:
        m = todo.pop()
        for x in _with_parents(m):
            if x not in seen:
                seen.add(x)
                todo.extend(EDGES.get(x, ()))
    _closure_cache[mod] = seen
    return seen


_importers_cache: dict[str, set[int]] = {}


def importers(mod: str) -> set[int]:
    """Tests that fail if importing `mod` fails: every test in a test file whose import
    (transitively) imports it, and every test that imported it while running."""
    if mod not in _importers_cache:
        out: set[int] = set()
        for m, f in MODFILE.items():
            if f in by_file and mod in closure(m):
                out |= set(by_file[f])
        for c, mods in RUNTIME.items():
            if any(mod in closure(m) for m in mods):
                out |= expand(c)[0]
        _importers_cache[mod] = out
    return _importers_cache[mod]


def expand(ctx: str) -> tuple[set[int], bool]:
    """context -> (tests, is_import_time)"""
    if not ctx:
        return set(), True
    kind, _, nodeid = ctx.partition("|")
    if kind == "I":
        return importers(nodeid), False
    if kind == "C":
        n = num.get(nodeid)
        return ({n} if n is not None else set()), False
    if kind in ("S", "T"):
        return set(by_prefix.get(nodeid.rsplit("::", 1)[0], [])) or set(by_file.get(nodeid.split("::")[0], [])), False
    if kind == "M":
        # code run while a test module is imported affects only that module's tests
        return set(by_file.get(nodeid, [])), False
    return set(), True


# ---------------------------------------------------------------- function spans
SAME_LINE_BODY: set = set()  # (file, start, end, line): a body sharing the def's last line


def spans_for(path: str) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    except (SyntaxError, FileNotFoundError, UnicodeDecodeError):
        return []
    out = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in ch.decorator_list] + [ch.lineno])
                q = f"{prefix}{ch.name}"
                body0 = ch.body[0]
                # skip a leading docstring: it is not executed per call
                if (isinstance(body0, ast.Expr) and isinstance(getattr(body0, "value", None), ast.Constant)
                        and isinstance(body0.value.value, str) and len(ch.body) > 1):
                    body0 = ch.body[1]
                out.append((start, ch.end_lineno, q, body0.lineno))
                hdr = max([ch.lineno] + [getattr(x, "end_lineno", ch.lineno) or ch.lineno for x in
                                         [ch.args, ch.returns] + ch.args.defaults + ch.args.kw_defaults if x is not None])
                if body0.lineno <= hdr:
                    SAME_LINE_BODY.add((path, start, ch.end_lineno, body0.lineno))
                walk(ch, q + ".")
            elif isinstance(ch, ast.ClassDef):
                walk(ch, f"{prefix}{ch.name}.")
            else:
                walk(ch, prefix)

    walk(tree, "")
    return out


def innermost(spans, line):
    best = None
    for s, e, q, b in spans:
        if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])):
            best = (s, e, q, b)
    return best


# ---------------------------------------------------------------- coverage
db = sqlite3.connect(os.path.join(ROOT, ".coverage"))
ctx = {cid: c for cid, c in db.execute("select id, context from context")}


def rel(p: str) -> str:
    root = os.path.abspath(ROOT) + os.sep
    if p.startswith(root):
        return os.path.relpath(p, root)
    i = p.find("/sqlglot/sqlglot/")
    return p[i + len("/sqlglot/"):] if i >= 0 else os.path.relpath(p, ROOT)


files = {fid: rel(p) for fid, p in db.execute("select id, path from file")}
SPANS = {f: spans_for(f) for f in files.values()}

file_tests: dict[str, set[int]] = defaultdict(set)
body_line_tests: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
cls_line_tests: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
unit_tests: dict[tuple[str, int, int], set[int]] = defaultdict(set)
unit_import: set[tuple[str, int, int]] = set()

imp_line_mods: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))

for fid, cid, bits in db.execute("select file_id, context_id, numbits from line_bits"):
    f = files[fid]
    tset, imp = expand(ctx[cid])
    if ctx[cid].startswith("I|"):
        # ran while a module was being imported: kept per line (which imports ran it),
        # apart from runtime coverage; an edit adds those modules' importers only when
        # it can change what the import does
        ymod = ctx[cid][2:]
        for line in numbits_to_nums(bits):
            imp_line_mods[f][line].add(ymod)
    else:
        file_tests[f] |= tset
    sp = SPANS[f]
    seen_units = set()
    lines_here = numbits_to_nums(bits)
    if ctx[cid].startswith("I|"):
        # executing a `def` during an import marks a body that shares the def's last
        # line (e.g. `def f(...) -> T: ...`); that is not a call of the function
        lines_here = [l for l in lines_here if not any((f, s0, e, l) in SAME_LINE_BODY for s0, e, q, b in sp)]
    if tset and not imp:
        for line in lines_here:
            if not any(b <= line <= e for s0, e, q, b in sp):
                if not ctx[cid].startswith("I|"):  # import-time: see imp_line_mods
                    cls_line_tests[f][line] |= tset
            else:
                body_line_tests[f][line] |= tset
    for line in lines_here:
        # credit every function whose BODY contains this executed line
        for s, e, q, b in sp:
            if b <= line <= e:
                k2 = (f, s, e)
                if (k2, cid) in seen_units:
                    continue
                seen_units.add((k2, cid))
                unit_tests[k2] |= tset
                if imp:
                    unit_import.add(k2)

# ---------------------------------------------------------------- usage + fixtures
use_tests: dict[str, set[int]] = defaultdict(set)
for c, mods in usage["uses"].items():
    if c.startswith("I|"):
        continue  # use during an import: see `touched`
    tset, _ = expand(c)
    for m in mods:
        use_tests[m] |= tset
class_tests: dict[str, set[int]] = defaultdict(set)
touched: dict[str, set[str]] = defaultdict(set)  # class -> imports that used or defined it
for c, cls in usage.get("classes", {}).items():
    if c.startswith("I|"):
        for k in cls:
            touched[k].add(c[2:])
        continue
    tset, _ = expand(c)
    for k in cls:
        class_tests[k] |= tset
# Classes merely *defined* during an import are not counted: their creation hooks
# (metaclasses, __init_subclass__) derive state that runtime users then observe, and
# a safe constant change (literals, names that already existed) does not make them raise.
fix_tests: dict[str, set[int]] = defaultdict(set)
for c, fx in usage["fixtures"].items():
    tset, _ = expand(c)
    for f in fx:
        fix_tests[f] |= tset

# ---------------------------------------------------------------- test-file spans
test_units = {}
for tf in by_file:
    sp = spans_for(tf)
    test_units[tf] = [[s, e, q] for s, e, q, b in sp]

def docstring_lines(path):
    try:
        tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    except (SyntaxError, FileNotFoundError, UnicodeDecodeError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            b0 = node.body[0]
            if isinstance(b0, ast.Expr) and isinstance(getattr(b0, "value", None), ast.Constant) and isinstance(b0.value.value, str):
                out.append([b0.lineno, b0.end_lineno])
    return out


# ---------------------------------------------------------------- write
all_src = sorted(set(file_tests) | set(use_tests) | set(SPANS))
_SETS: dict = {}


def I(v) -> int:
    """Intern a test set: the maps store each distinct set once and refer to it by index."""
    return _SETS.setdefault(tuple(sorted(v)), len(_SETS))


maps = {
    "file_cov": {f: I(file_tests[f]) for f in all_src if file_tests.get(f)},
    "file_use": {f: I(use_tests[f]) for f in all_src if use_tests.get(f)},
    "fixtures": {f: I(v) for f, v in fix_tests.items()},
    "class_use": {k: I(v) for k, v in class_tests.items()},
    "spans": {f: [[s, e, b] for s, e, _, b in SPANS[f]] for f in SPANS},
    "units": [[f, s, e, I(v), int((f, s, e) in unit_import)] for (f, s, e), v in unit_tests.items()],
    "test_spans": test_units,
    "docstrings": {f: docstring_lines(f) for f in set(SPANS) | set(by_file)},
    "cls_lines": {f: {str(l): I(v) for l, v in d.items()} for f, d in cls_line_tests.items()},
}
_mods = sorted(set(MODFILE) | set(EDGES) | {y for d in imp_line_mods.values() for v in d.values() for y in v}
               | {y for v in touched.values() for y in v})
_mi = {m: i for i, m in enumerate(_mods)}
maps["imp_mods"] = _mods
maps["imp_importers"] = [I(importers(m)) for m in _mods]
maps["imp_line_mods"] = {f: {str(l): sorted(_mi[y] for y in v) for l, v in d.items()} for f, d in imp_line_mods.items()}
maps["touched"] = {k: sorted(_mi[y] for y in v) for k, v in touched.items()}
maps["sets"] = [list(t) for t in _SETS]
import zlib as _z

open(os.path.join(OUT, "maps.json.z"), "wb").write(_z.compress(json.dumps(maps, separators=(",", ":")).encode(), 9))

# for build_cmp.py only (not shipped): tests behind every "I|module" context
json.dump({m: sorted(importers(m)) for m in set(MODFILE) | set(EDGES)},
          open(os.path.join(ROOT, "importers.json"), "w"))

# per-line runtime tests for lines inside function bodies, deduplicated, zlib-compressed
import zlib

sets: dict[frozenset, int] = {}
lines_out = {}
for f, d in body_line_tests.items():
    lines_out[f] = {str(l): sets.setdefault(frozenset(v), len(sets)) for l, v in d.items()}
blob = json.dumps({"sets": [sorted(x) for x in sets], "lines": lines_out}, separators=(",", ":")).encode()
open(os.path.join(OUT, "lines.json.z"), "wb").write(zlib.compress(blob, 9))
print("line map: lines", sum(len(v) for v in lines_out.values()), "sets", len(sets),
      "bytes", os.path.getsize(os.path.join(OUT, "lines.json.z")))
print("units", len(unit_tests), "import-time units", len(unit_import),
      "files", len(maps["file_cov"]), "fixtures", len(maps["fixtures"]),
      "bytes", os.path.getsize(os.path.join(OUT, "maps.json.z")), "distinct sets", len(_SETS))
