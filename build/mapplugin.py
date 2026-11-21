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


# ---------------------------------------------------------------- imports
# Code that runs while a module is being imported counts for every test that imports
# that module (a failing import fails all of them), not for whichever test happened to
# trigger the import first. Each first import of a sqlglot or tests module runs under
# the context "I|<module>", and every import request is recorded:
#   EDGES[importer] = modules requested while `importer` was being imported
#   RUNTIME[ctx]    = modules requested while a test phase ran (outside any import)
import builtins as _b
import importlib as _il
import importlib.abc as _ilabc

STACK: list = []
CREATED: dict = {}  # module -> classes (with their bases) defined while it was imported
EDGES: dict = {}
RUNTIME: dict = {}
MODFILE: dict = {}
_OURS = ("sqlglot", "tests")


def _ours(name):
    return name.split(".")[0] in _OURS


def _note(name):
    if not name or not _ours(name):
        return
    if STACK:
        EDGES.setdefault(STACK[-1], set()).add(name)
    else:
        RUNTIME.setdefault(CUR[0], set()).add(name)


_real_import = _b.__import__


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    mod = _real_import(name, globals, locals, fromlist, level)
    try:
        full = name
        if level:
            pkg = (globals or {}).get("__package__") or ""
            parts = pkg.split(".")
            base = ".".join(parts[: len(parts) - (level - 1)]) if level > 1 else pkg
            full = f"{base}.{name}" if name else base
        _note(full)
        for item in fromlist or ():
            sub = f"{full}.{item}"
            if sub in sys.modules:
                _note(sub)
            else:  # `from pkg import Name`: a package __getattr__ may have loaded a module
                obj = getattr(sys.modules.get(full), item, None)
                m = getattr(obj, "__module__", None)
                if isinstance(m, str):
                    _note(m)
    except Exception:  # noqa: BLE001 - recording must never change behaviour
        pass
    return mod


_b.__import__ = _import
_real_import_module = _il.import_module


def _import_module(name, package=None):
    mod = _real_import_module(name, package)
    try:
        _note(mod.__name__)
    except Exception:  # noqa: BLE001
        pass
    return mod


_il.import_module = _import_module


class _CtxLoader(_ilabc.Loader):
    """Per-module wrapper: runs the real loader under the module's import context.
    (Loaders can be shared between modules, e.g. pytest's rewriting hook, so the
    real loader is never modified.)"""

    def __init__(self, inner, name):
        self._inner, self._name = inner, name

    def __getattr__(self, attr):
        return getattr(self._inner, attr)

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        _note(self._name)
        f = getattr(module, "__file__", None)
        if f:
            MODFILE[self._name] = f
        prev = CUR[0]
        STACK.append(self._name)
        _set("I|" + self._name)
        try:
            self._inner.exec_module(module)
        finally:
            STACK.pop()
            _set(prev)
        try:
            got = CREATED.setdefault(self._name, set())
            todo = [v for v in vars(module).values() if isinstance(v, type) and v.__module__ == self._name]
            seen = set()
            while todo:
                c = todo.pop()
                if c in seen:
                    continue
                seen.add(c)
                for k in c.__mro__:
                    if k.__module__.startswith("sqlglot"):
                        got.add(k.__module__ + ":" + k.__qualname__)
                todo.extend(v for v in vars(c).values() if isinstance(v, type) and v.__module__ == self._name)
        except Exception:  # noqa: BLE001
            pass


class _ImportContext(_ilabc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not _ours(fullname):
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        if loader is None or isinstance(loader, _CtxLoader) or not hasattr(loader, "exec_module"):
            return spec
        spec.loader = _CtxLoader(loader, fullname)
        return spec


sys.meta_path.insert(0, _ImportContext())


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


def pytest_collection(session):
    # pytest's assertion-rewriting finder goes to the front of sys.meta_path at start-up;
    # put ours back in front so test modules get import contexts too (ours delegates).
    for f in list(sys.meta_path):
        if isinstance(f, _ImportContext):
            sys.meta_path.remove(f)
            sys.meta_path.insert(0, f)


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
        "edges": {k: sorted(v) for k, v in EDGES.items()},
        "runtime_imports": {k: sorted(v) for k, v in RUNTIME.items()},
        "modfile": {k: os.path.relpath(v, _ROOT) for k, v in MODFILE.items()},
        "created": {k: sorted(
            mods[c.split(":")[0]] + "::" + c.split(":", 1)[1] for c in v if c.split(":")[0] in mods
        ) for k, v in CREATED.items()},
    }
    with _real_open(os.environ.get("MAPPLUGIN_OUT", "usage.json"), "w") as fh:
        json.dump(out, fh)
