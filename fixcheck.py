"""Line-level fixture check (v25), run at decision time on the pinned sqlglot.

For a commit whose only changes are edits to SQL fixture files that some tests read
through tests/helpers.py's loaders, run each reading test's own method on the pinned
code, with those loaders (in the test module) yielding only the entries the patch adds
or changes (entries are compared whole: meta lines, SQL and expected output, so any
re-pairing shows up as new entries). Removed entries cannot fail a check.
The fixture files are not on disk here: they are served to tests/helpers.py only, so a
test reading a fixture any other way fails and the caller falls back to v21.
Any failure, error, unknown construct or budget overrun -> the caller selects as v21.
"""
import contextlib, io, marshal, os, pickle, signal, sys, types, zlib
from collections import Counter
from concurrent.futures import Future

HERE = os.path.dirname(os.path.abspath(__file__))
PINNED_DIR = os.path.join(HERE, "pinned")
FIXTURES = os.path.join(HERE, "maps", "fixtures.bin.z")

# test id -> (module, class, method); fixture readers handled through the loaders
METHODS = {}
for _m in ("isolate_table_selects", "qualify_tables", "normalize", "qualify_columns",
           "qualify_columns__with_invisible", "pushdown_cte_alias_columns", "normalize_identifiers",
           "quote_identifiers", "pushdown_projection", "simplify", "unnest_subqueries",
           "pushdown_predicates", "optimize_joins", "eliminate_joins", "eliminate_ctes",
           "eliminate_subqueries", "canonicalize_internal_names", "canonicalize",
           "annotate_types", "annotate_funcs", "qualify_columns__invalid", "optimize",
           "merge_subqueries"):
    METHODS[f"tests/test_optimizer.py::TestOptimizer::test_{_m}"] = ("tests.test_optimizer", "TestOptimizer", f"test_{_m}")
for _m in ("identity", "partial", "pretty"):
    METHODS[f"tests/test_transpile.py::TestTranspile::test_{_m}"] = ("tests.test_transpile", "TestTranspile", f"test_{_m}")
METHODS["tests/test_serde.py::TestSerde::test_serde"] = ("tests.test_serde", "TestSerde", "test_serde")


class _Budget(BaseException):
    pass


class _SyncPool:
    """ProcessPoolExecutor stand-in: same pickling of the task and its result."""

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except _Budget:
            raise
        except BaseException as e:  # noqa: BLE001 - the test would see it via future.result()
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass


_state = None


def _setup():
    global _state
    if _state is not None:
        return _state
    _state = False
    for name in ("duckdb", "pandas", "pandas.testing"):
        mod = types.ModuleType(name)
        if name == "pandas.testing":
            def assert_frame_equal(*a, **k):
                raise RuntimeError("no pandas here")
            mod.assert_frame_equal = assert_frame_equal
        if name == "duckdb":
            def connect(*a, **k):
                raise RuntimeError("no duckdb here")
            mod.connect = connect
        sys.modules.setdefault(name, mod)
    sys.modules["pandas"].testing = sys.modules["pandas.testing"]
    sys.path.insert(0, PINNED_DIR)
    import tests.helpers as helpers
    fixtures = marshal.loads(zlib.decompress(open(FIXTURES, "rb").read()))
    _state = {"helpers": helpers, "fixtures": fixtures, "texts": {}}

    def fake_open(path, *a, **k):
        rel = os.path.relpath(path, helpers.FIXTURES_DIR).replace(os.sep, "/")
        text = _state["texts"].get(rel)
        if text is None:
            raise FileNotFoundError(path)
        return io.StringIO(text)

    helpers.open = fake_open
    return _state


def _universal(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def apply_blocks(old_text, blocks):
    """Apply parse_patch blocks to the pinned text; None if anything does not match."""
    lines = old_text.split("\n")
    out, pos = [], 0  # pos: 0-based index of the next pinned line to copy
    for first, removed, added in blocks:
        start = first - 1
        if start < pos or start + len(removed) > len(lines):
            return None
        out.extend(lines[pos:start])
        if lines[start:start + len(removed)] != removed:
            return None
        out.extend(added)
        pos = start + len(removed)
    out.extend(lines[pos:])
    return "\n".join(out)


def _items(helpers, rel, text, pairs):
    _state["texts"][rel] = text
    if pairs:
        return [((tuple(sorted(m.items())), s, e), (m, s, e)) for m, s, e in helpers.load_sql_fixture_pairs(rel)]
    return [(s, s) for s in helpers.load_sql_fixtures(rel)]


def _alarm(signum, frame):
    raise _Budget()


def check(edits, readers, budget):
    """edits: {fixture rel path (under tests/fixtures/): new text}; readers: test ids.
    True only if every reader's own method passes on the new entries."""
    st = _setup()
    if not st:
        return False
    helpers, fixtures = st["helpers"], st["fixtures"]
    import importlib, unittest
    old_handler = signal.signal(signal.SIGPROF, _alarm)
    signal.setitimer(signal.ITIMER_PROF, budget)
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            new_items = {}
            for rel, new_text in edits.items():
                for pairs in (True, False):
                    old = _items(helpers, rel, fixtures[rel], pairs)
                    new = _items(helpers, rel, new_text, pairs)
                    left = Counter(k for k, _ in old)
                    fresh = []
                    for k, v in new:
                        if left[k] > 0:
                            left[k] -= 1
                        else:
                            fresh.append(v)
                    new_items[(rel, pairs)] = fresh
            st["texts"].clear()
            st["texts"].update(fixtures)
            st["texts"].update(edits)

            def loader(pairs):
                def load(filename):
                    if (filename, pairs) in new_items:
                        return iter(list(new_items[(filename, pairs)]))
                    # an unedited fixture: every entry passed at the pinned commit, and
                    # nothing it depends on changed, so none needs running again
                    return iter([])
                return load

            for tid in sorted(readers):
                modname, cls, meth = METHODS[tid]
                mod = importlib.import_module(modname)
                saved = {k: mod.__dict__.get(k) for k in ("load_sql_fixture_pairs", "load_sql_fixtures")}
                try:
                    mod.load_sql_fixture_pairs = loader(True)
                    mod.load_sql_fixtures = loader(False)
                    klass = getattr(mod, cls)
                    inst = klass(meth)
                    inst.pool = _SyncPool()
                    inst.conn = None
                    inst.setUp()
                    try:
                        getattr(inst, meth)()
                    finally:
                        inst.tearDown()
                        inst.doCleanups()
                finally:
                    for k, v in saved.items():
                        if v is None:
                            mod.__dict__.pop(k, None)
                        else:
                            setattr(mod, k, v)
        return True
    except BaseException as e:  # noqa: BLE001 - failure, error or budget: fall back
        if os.environ.get("BR_VERBOSE") == "1":
            import traceback
            sys.stderr.write("  fixcheck: " + "".join(traceback.format_exception(e))[-1500:] + "\n")
        return False
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old_handler)
        st["texts"].clear()
