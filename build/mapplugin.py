"""pytest plugin used ONLY while building the maps from the pinned sqlglot commit.

Run as:  coverage run --rcfile=build/coveragerc -m pytest tests/ -p mapplugin
It records, for every phase of every test:
  * coverage, under a context named after the test (coverage.switch_context);
  * which sqlglot classes the test instantiated (dialects, parsers, generators,
    tokenizers, expressions) -> the modules that define them and their bases;
  * which files under tests/fixtures/ the test opened.
Process pools are replaced by an in-process executor so work done "in a worker"
is attributed to the test that asked for it.
"""
from __future__ import annotations

import builtins
import concurrent.futures as _cf
import io
import json
import os
import pickle
import sys
from concurrent.futures import Future

# ---------------------------------------------------------------- run pools inline
def _roundtrip(obj):
    """Pickle and unpickle, as crossing a real process boundary would: sqlglot
    expressions pickle through sqlglot.serde, which must count as used."""
    return pickle.loads(pickle.dumps(obj))


class _InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*_roundtrip(tuple(initargs)))

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            fn2, args2, kwargs2 = _roundtrip((fn, args, kwargs))
            fut.set_result(_roundtrip(fn2(*args2, **kwargs2)))
        except BaseException as exc:  # noqa: BLE001 - mirror executor semantics
            fut.set_exception(exc)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return map(fn, *iterables)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


class _InlineThreadExecutor(_InlineExecutor):
    def __init__(self, max_workers=None, thread_name_prefix="", initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut


_cf.ProcessPoolExecutor = _InlineExecutor
_cf.ThreadPoolExecutor = _InlineThreadExecutor

# ---------------------------------------------------------------- state
CUR = [""]
USES: dict[str, set[str]] = {}
CLASSES: dict[str, set[str]] = {}
FIXTURES: dict[str, set[str]] = {}
_ROOT = os.getcwd()
_FIX = os.path.join(_ROOT, "tests", "fixtures") + os.sep


def _cov():
    try:
        import coverage

        return coverage.Coverage.current()
    except Exception:  # noqa: BLE001
        return None


def _set(key: str) -> None:
    CUR[0] = key
    cov = _cov()
    if cov is not None:
        cov.switch_context(key)


# ---------------------------------------------------------------- fixture reads
_real_open = builtins.open


def _open(file, *a, **kw):
    try:
        p = os.path.abspath(os.fspath(file))
        if p.startswith(_FIX) and CUR[0]:
            FIXTURES.setdefault(CUR[0], set()).add(os.path.relpath(p, _ROOT))
    except TypeError:
        pass
    return _real_open(file, *a, **kw)


builtins.open = _open
io.open = _open


# ---------------------------------------------------------------- class usage
def _record_type(t: type) -> None:
    key = CUR[0]
    if not key:
        return
    s = USES.setdefault(key, set())
    c = CLASSES.setdefault(key, set())
    for k in t.__mro__:
        m = k.__module__
        if m.startswith("sqlglot"):
            s.add(m)
            c.add(m + ":" + k.__qualname__)


def _wrap_init(cls):
    orig = cls.__init__

    def __init__(self, *a, **kw):
        _record_type(type(self))
        return orig(self, *a, **kw)

    __init__.__wrapped__ = orig
    cls.__init__ = __init__


def _install_class_hooks():
    import sqlglot  # noqa: F401  (import here, after open() is patched)
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr as Expression  # wrap the class that owns __init__
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    _off = set(os.environ.get("MAP_DISABLE", "").split(","))
    for name, c in (("expr", Expression), ("parser", Parser), ("gen", Generator), ("tok", Tokenizer)):
        if name not in _off:
            _wrap_init(c)

    def _dialect_used(d):
        k = d if isinstance(d, type) else type(d)
        _record_type(k)
        for attr in ("parser_class", "generator_class", "tokenizer_class", "jsonpath_tokenizer_class"):
            sub = getattr(k, attr, None)
            if isinstance(sub, type):
                _record_type(sub)

    orig_dinit = Dialect.__init__

    def dinit(self, *a, **kw):
        _dialect_used(self)
        return orig_dinit(self, *a, **kw)

    if "dinit" not in _off:
        Dialect.__init__ = dinit

    # Note: do not wrap the _Dialect metaclass (its __eq__ calls cls.get -> recursion).
    gor = getattr(Dialect, "get_or_raise", None)
    if gor is not None and "gor" not in _off:
        orig_gor = gor.__func__

        def get_or_raise(cls, *a, **kw):
            r = orig_gor(cls, *a, **kw)
            if r is not None:
                _dialect_used(r)
            return r

        Dialect.get_or_raise = classmethod(get_or_raise)


_install_class_hooks()


# ---------------------------------------------------------------- pytest hooks
import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if collector.__class__.__name__ == "Module":
        _set("M|" + collector.nodeid)
    yield
    _set("")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set("S|" + item.nodeid)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set("C|" + item.nodeid)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _set("T|" + item.nodeid)
    yield
    _set("")


def pytest_sessionfinish(session, exitstatus):
    mods = {}
    for name, m in list(sys.modules.items()):
        f = getattr(m, "__file__", None)
        if name.startswith("sqlglot") and f:
            mods[name] = os.path.relpath(f, _ROOT)
    out = {
        "uses": {k: sorted(mods[m] for m in v if m in mods) for k, v in USES.items()},
        "fixtures": {k: sorted(v) for k, v in FIXTURES.items()},
        "classes": {k: sorted(
            mods[c.split(":")[0]] + "::" + c.split(":", 1)[1] for c in v if c.split(":")[0] in mods
        ) for k, v in CLASSES.items()},
        "nodeids": [i.nodeid for i in session.items],
    }
    with _real_open(os.environ.get("MAPPLUGIN_OUT", "usage.json"), "w") as fh:
        json.dump(out, fh)
