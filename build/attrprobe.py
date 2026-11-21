"""pytest plugin used ONLY while building the maps (load it BEFORE mapplugin).

Records, per test, which class's definition each read of a class-level UPPERCASE
constant resolves to: every `X.NAME` load in sqlglot is rewritten to call
__attr_probe__, which looks NAME up along type(X).__mro__ (or X.__mro__ for a class)
and records the defining class. An edit to `NAME = ...` in class C can then reach only
tests that read C's definition (directly, or through a subclass that does not
override it).

Pass-through: when `kw=X.NAME` is a keyword argument of a call to a sqlglot class whose
__init__ only stores that parameter (`self.kw = kw`), the read is not recorded there;
instead every later load of the stored attribute (`.kw`) is recorded as "pass:kw". So
an edit reaches the tests that USE the stored value, e.g. the tokenizer core using
heredoc_tag_is_identifier only while scanning heredoc strings.
"""
from __future__ import annotations

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import re
import sys

_ROOT = os.getcwd()
_PKG = os.path.join(_ROOT, "sqlglot") + os.sep
_UPPER = re.compile(r"^_?[A-Z][A-Z0-9_]{2,}$")
READS: dict = {}   # (owner key, NAME) -> set(ctx)
PASS: dict = {}    # attr -> set(ctx)
FILES: set = set()
_mp = [None]


def _ctx():
    m = _mp[0] or sys.modules.get("mapplugin")
    _mp[0] = m
    return m.CUR[0] if m is not None else ""


def _owner(k):
    mod = sys.modules.get(k.__module__)
    f = getattr(mod, "__file__", None)
    return f"{os.path.relpath(f, _ROOT)}::{k.__qualname__}" if f else f"?{k.__module__}::{k.__qualname__}"


def __attr_probe__(obj, name):
    v = getattr(obj, name)
    try:
        if isinstance(obj, super):
            mro = obj.__self_class__.__mro__
        elif isinstance(obj, type):
            mro = obj.__mro__
        else:
            d = getattr(obj, "__dict__", None)
            if isinstance(d, dict) and name in d:
                return v  # an instance attribute: not a class constant read
            mro = type(obj).__mro__
        for k in mro:
            if name in k.__dict__:
                READS.setdefault((_owner(k), name), set()).add(_ctx())
                break
    except Exception:  # noqa: BLE001
        READS.setdefault(("?", name), set()).add(_ctx())
    return v


def __attr_pass__(obj, name):
    return getattr(obj, name)


def __attr_use__(obj, attr):
    PASS.setdefault(attr, set()).add(_ctx())
    return getattr(obj, attr)


builtins.__attr_probe__ = __attr_probe__
builtins.__attr_pass__ = __attr_pass__
builtins.__attr_use__ = __attr_use__


def _scan():
    """Class-level UPPERCASE names, and constructor parameters stored unchanged."""
    names, stores = set(), {}
    for r, _, fs in os.walk(_PKG):
        for f in fs:
            if not f.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(r, f), encoding="utf-8").read())
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    for b in n.body:
                        for t in (b.targets if isinstance(b, ast.Assign) else [b.target] if isinstance(b, ast.AnnAssign) else []):
                            if isinstance(t, ast.Name) and _UPPER.match(t.id):
                                names.add(t.id)
                    for b in n.body:
                        if isinstance(b, ast.FunctionDef) and b.name == "__init__":
                            params = {a.arg for a in b.args.args + b.args.kwonlyargs}
                            for p in params:
                                uses = [x for x in ast.walk(b) if isinstance(x, ast.Name) and x.id == p]
                                st = [s for s in ast.walk(b) if isinstance(s, (ast.Assign, ast.AnnAssign))
                                      and isinstance(s.value, ast.Name) and s.value.id == p
                                      and len(s.targets if isinstance(s, ast.Assign) else [s.target]) == 1
                                      and isinstance((s.targets[0] if isinstance(s, ast.Assign) else s.target), ast.Attribute)
                                      and isinstance((s.targets[0] if isinstance(s, ast.Assign) else s.target).value, ast.Name)
                                      and (s.targets[0] if isinstance(s, ast.Assign) else s.target).value.id == "self"]
                                if len(st) == 1 and len(uses) == 1:  # used only to be stored
                                    t = st[0].targets[0] if isinstance(st[0], ast.Assign) else st[0].target
                                    stores.setdefault(p, set()).add(t.attr)
    # keywords that some call actually gives a class constant (kw=X.NAME)
    fed = set()
    for r, _, fs in os.walk(_PKG):
        for f in fs:
            if f.endswith(".py"):
                for n in ast.walk(ast.parse(open(os.path.join(r, f), encoding="utf-8").read())):
                    if isinstance(n, ast.Call):
                        for k in n.keywords:
                            if k.arg and isinstance(k.value, ast.Attribute) and k.value.attr in names:
                                fed.add(k.arg)
    # a keyword is a pass-through only if every __init__ taking it stores it the same way
    passkw = {p: next(iter(a)) for p, a in stores.items() if len(a) == 1 and p in fed}
    return names, passkw


NAMES, PASSKW = _scan()
PASS_ATTRS = set(PASSKW.values())


class _T(ast.NodeTransformer):
    def visit_Call(self, node):
        new_kw = []
        for k in node.keywords:
            v = k.value
            if (k.arg in PASSKW and isinstance(v, ast.Attribute) and isinstance(v.ctx, ast.Load)
                    and v.attr in NAMES):
                v2 = ast.Call(ast.Name("__attr_pass__", ast.Load()), [self.visit(v.value), ast.Constant(v.attr)], [])
                new_kw.append(ast.copy_location(ast.keyword(k.arg, ast.copy_location(v2, v)), k))
            else:
                new_kw.append(self.visit(k))
        node.func = self.visit(node.func)
        node.args = [self.visit(a) for a in node.args]
        node.keywords = new_kw
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load):
            if node.attr in NAMES:
                return ast.copy_location(ast.Call(ast.Name("__attr_probe__", ast.Load()), [node.value, ast.Constant(node.attr)], []), node)
            if node.attr in PASS_ATTRS:
                return ast.copy_location(ast.Call(ast.Name("__attr_use__", ast.Load()), [node.value, ast.Constant(node.attr)], []), node)
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        tree = ast.fix_missing_locations(_T().visit(ast.parse(data, path)))
        FILES.add(os.path.relpath(path, _ROOT))
        return compile(tree, path, "exec", dont_inherit=True)

    def get_code(self, fullname):
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


assert "sqlglot" not in sys.modules, "load attrprobe before anything imports sqlglot"
sys.meta_path.insert(0, _Finder())


def pytest_sessionfinish(session, exitstatus):
    out = {"reads": [[o, n, sorted(c)] for (o, n), c in READS.items()],
           "pass": {a: sorted(c) for a, c in PASS.items()},
           "passkw": PASSKW, "names": sorted(NAMES), "files": sorted(FILES)}
    with open(os.environ.get("ATTRPROBE_OUT", "attrprobe.json"), "w") as fh:
        json.dump(out, fh)
