"""pytest plugin used ONLY while building the maps (load it after mapplugin).

Records, per test, which keys of each class-level constant dict of sqlglot are read
for their VALUE ([k], .get(k)), and which tests use a dict as a whole (iteration,
.items/.keys/.values, copies, equality, repr). An edit that changes only the value of
some entries (same keys) can reach only tests that read those keys' values or used the
dict as a whole.

Every sqlglot module is imported first, so the class-body copies ({**Base.X, ...}),
tries and metaclass work happen before recording; the selector treats those import-time
reads separately (copies flow into the subclass dicts, which are recorded too). The
one runtime cache built from such dicts, generator._DISPATCH_CACHE, is cleared before
every test phase so each test records its own reads.
"""
from __future__ import annotations

import enum
import json
import linecache
import os
import re
import sys

_ROOT = os.getcwd()
_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
GET: dict = {}     # dict id -> {key repr -> set(ctx)}: a key's value was read
WHOLE: dict = {}   # dict id -> set(ctx): values used as a whole (items/values/copy/==/repr)
EAGER: list = []   # modules loaded before recording started (their import-time reads are not recorded)
INFO: dict = {}    # dict id -> {"owners": [...], "name": N, "mro": [...]}
_mp = [None]


def _ctx():
    m = _mp[0]
    return m.CUR[0] if m is not None else ""


def krepr(k):
    if isinstance(k, type):
        return f"cls:{k.__module__}.{k.__qualname__}"
    if isinstance(k, enum.Enum):
        return f"enum:{type(k).__qualname__}.{k.name}"
    if isinstance(k, (str, int, float, bool, bytes, type(None))):
        return repr(k)
    return "other:" + type(k).__qualname__


def _class_body_copy(rid):
    """True when the read comes from a class body copying this dict into the class's
    own attribute of the same name ({**Base.N, ...}): those values flow into that
    subclass dict, which is wrapped and recorded itself."""
    f = sys._getframe(3)
    loc = f.f_locals
    if "__qualname__" not in loc or "__module__" not in loc:
        return False
    line = linecache.getline(f.f_code.co_filename, f.f_lineno)
    name = INFO[rid]["name"]
    return re.search(r"\*\*\s*[\w.]+\." + re.escape(name) + r"\b", line) is not None


class RecDict(dict):
    __slots__ = ("_rid",)

    def _get(self, k):
        if not _class_body_copy(self._rid):
            GET.setdefault(self._rid, {}).setdefault(krepr(k), set()).add(_ctx())

    def _whole(self):
        if not _class_body_copy(self._rid):
            WHOLE.setdefault(self._rid, set()).add(_ctx())

    def __getitem__(self, k):
        self._get(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._get(k)
        return dict.get(self, k, default)

    def setdefault(self, k, default=None):
        self._get(k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *a):
        self._get(k)
        return dict.pop(self, k, *a)

    # Iterating keys (for k in d, d.keys()) cannot see a value change, so it is not
    # recorded; but __iter__ and keys are overridden so that dict(d) and {**d} take the
    # slow path that reads every value through __getitem__ (recorded).
    def __iter__(self):
        return dict.__iter__(self)

    def keys(self):
        return dict.keys(self)

    def values(self):
        self._whole()
        return dict.values(self)

    def items(self):
        self._whole()
        return dict.items(self)

    def copy(self):
        self._whole()
        return dict(dict.items(self))

    def __eq__(self, o):
        self._whole()
        return dict.__eq__(self, o)

    def __ne__(self, o):
        self._whole()
        return dict.__ne__(self, o)

    __hash__ = None

    def __repr__(self):
        self._whole()
        return dict.__repr__(self)

    def __or__(self, o):
        self._whole()
        return dict(dict.items(self)) | o

    def __ror__(self, o):
        self._whole()
        return o | dict(dict.items(self))

    def __reduce_ex__(self, proto):
        self._whole()
        return (dict, (dict(dict.items(self)),))

    def popitem(self):
        self._whole()
        return dict.popitem(self)


def _owner_key(cls):
    mod = sys.modules.get(cls.__module__)
    f = getattr(mod, "__file__", None)
    return f"{os.path.relpath(f, _ROOT)}::{cls.__qualname__}" if f else None


_MEMO: dict = {}


def _wrap_module(name):
    m = sys.modules.get(name)
    if m is None:
        return
    todo = [v for v in vars(m).values() if isinstance(v, type) and v.__module__ == name]
    seen = set()
    while todo:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        todo.extend(v for v in vars(c).values() if isinstance(v, type) and v.__module__ == name)
        ok = _owner_key(c)
        if ok is None:
            continue
        for n, v in list(vars(c).items()):
            if type(v) is dict and _NAME.match(n):
                r = _MEMO.get(id(v))
                if r is None:
                    r = RecDict(v)
                    r._rid = len(INFO)
                    _MEMO[id(v)] = (r, v)  # keep the original alive so its id stays unique
                    INFO[r._rid] = {"owners": [], "name": n, "mro": [], "keys": sorted({krepr(k) for k in v})}
                else:
                    r = r[0]
                inf = INFO[r._rid]
                inf["owners"].append(ok)
                inf["mro"] = sorted(set(inf["mro"]) | {k for k in (_owner_key(b) for b in c.__mro__) if k})
                setattr(c, n, r)
    for n, v in list(vars(m).items()):
        if type(v) is dict and id(v) in _MEMO:
            setattr(m, n, _MEMO[id(v)][0])


def _install():
    mp = sys.modules.get("mapplugin")
    _mp[0] = mp
    for name in sorted(sys.modules):
        if name == "sqlglot" or name.startswith("sqlglot."):
            EAGER.append(name)
            _wrap_module(name)
    # every later sqlglot import: wrap that module's dicts right after it runs
    loader = getattr(mp, "_CtxLoader", None)
    if loader is not None:
        orig = loader.exec_module

        def exec_module(self, module, _orig=orig):
            _orig(self, module)
            if self._name == "sqlglot" or self._name.startswith("sqlglot."):
                _wrap_module(self._name)

        loader.exec_module = exec_module


_install()


def _clear_caches():
    g = sys.modules.get("sqlglot.generator")
    cache = getattr(g, "_DISPATCH_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()


import pytest  # noqa: E402


def pytest_configure(config):
    _mp[0] = sys.modules.get("mapplugin")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _clear_caches()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _clear_caches()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    _clear_caches()


def pytest_sessionfinish(session, exitstatus):
    out = {str(i): {**INFO[i], "get": {k: sorted(v) for k, v in GET.get(i, {}).items()},
                    "whole": sorted(WHOLE.get(i, set()))} for i in INFO}
    out = {"dicts": out, "eager": EAGER}
    with open(os.environ.get("DICTPROBE_OUT", "dictprobe.json"), "w") as fh:
        json.dump(out, fh)
