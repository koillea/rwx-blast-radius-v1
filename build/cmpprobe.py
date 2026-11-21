"""pytest plugin used ONLY while building the maps (load it before mapplugin).

For every single-operator comparison in sqlglot using is / is not / == / != / < / <= /
> / >=, it records which tests ever evaluated it with operands for which the
"partner" operator (is<->==, is not<->!=, <<-><=, ><->>=) would give a different
result. An edit that swaps a comparison for its partner can change behaviour only in
those tests. Sites are keyed by (file, line, column) in the pinned source.
"""
from __future__ import annotations

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys

_ROOT = os.getcwd()
_PKG = os.path.join(_ROOT, "sqlglot") + os.sep
PARTNER = {ast.Is: ("is", operator.is_, operator.eq), ast.Eq: ("==", operator.eq, operator.is_),
           ast.IsNot: ("is not", operator.is_not, operator.ne), ast.NotEq: ("!=", operator.ne, operator.is_not),
           ast.Lt: ("<", operator.lt, operator.le), ast.LtE: ("<=", operator.le, operator.lt),
           ast.Gt: (">", operator.gt, operator.ge), ast.GtE: (">=", operator.ge, operator.gt)}
SITES: list = []          # sid -> [file, line, col, op]
DIFF: dict = {}           # sid -> set(context keys)
FILES: set = set()
_mp = [None]


_IN_ALT = [False]
_ALT_SID = [-1]
_REENTERED = [False]


def __cmp_probe__(sid, a, b):
    site = SITES[sid]
    if _IN_ALT[0]:  # evaluating a partner operator: not part of the real run
        if sid == _ALT_SID[0]:
            # The partner operator's own code runs this same comparison again: with the
            # swap applied, that inner evaluation would use the partner too (and may
            # recurse), which this probe cannot see. Count the evaluation as differing.
            _REENTERED[0] = True
        return site[4](a, b)
    r = site[4](a, b)
    if site[6] and a is not b and type(a) is type(b) and getattr(type(a), "arg_types", None) is not None:
        same = False  # == on two sqlglot nodes hashes whole trees: assume it may differ
    else:
        _IN_ALT[0] = True
        _ALT_SID[0] = sid
        _REENTERED[0] = False
        try:
            alt = site[5](a, b)
            same = (alt is r or (type(alt) is bool and type(r) is bool and alt == r)) and not _REENTERED[0]
        except BaseException:  # noqa: BLE001
            same = False
        finally:
            _IN_ALT[0] = False
            _ALT_SID[0] = -1
    if not same:
        m = _mp[0]
        DIFF.setdefault(sid, set()).add(m.CUR[0] if m is not None else "")
    return r


builtins.__cmp_probe__ = __cmp_probe__


class _T(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in PARTNER:
            return node
        name, op, alt = PARTNER[type(node.ops[0])]
        sid = len(SITES)
        SITES.append([self.rel, node.lineno, node.col_offset, name, op, alt, alt is operator.eq or alt is operator.ne])
        call = ast.Call(ast.Name("__cmp_probe__", ast.Load()), [ast.Constant(sid), node.left, node.comparators[0]], [])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(path, _ROOT)
        tree = ast.parse(data, path)
        tree = ast.fix_missing_locations(_T(rel).visit(tree))
        FILES.add(rel)
        return compile(tree, path, "exec", dont_inherit=True)

    def get_code(self, fullname):  # skip bytecode caches
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py") or not spec.origin.startswith(_PKG):
            return spec
        spec.loader = _Loader(fullname, spec.origin)
        return spec


assert "sqlglot" not in sys.modules, "load cmpprobe before anything imports sqlglot"
sys.meta_path.insert(0, _Finder())


def pytest_configure(config):
    _mp[0] = sys.modules.get("mapplugin")


def pytest_sessionfinish(session, exitstatus):
    out = {"files": sorted(FILES), "diff": {f"{s[0]}:{s[1]}:{s[2]}:{s[3]}": sorted(DIFF[i]) for i, s in enumerate(SITES) if i in DIFF},
           "sites": len(SITES)}
    with open(os.environ.get("CMPPROBE_OUT", "cmpprobe.json"), "w") as fh:
        json.dump(out, fh)
