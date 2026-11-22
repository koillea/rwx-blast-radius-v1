"""Decision-time check for tests/test_executor.py::TestExecutor::test_execute_tpcds.

That test runs every TPC-DS query (maps/tpcds/queries.json, copied from the pinned
fixture) as: parse_one(sql), pickled to a worker; for queries marked "execute":
execute(expression, schema=TPCDS_SCHEMA, tables) = ensure_schema -> optimize(...,
leave_tables_isolated=True) -> Plan -> PythonExecutor over fixed tables; and compares
with DuckDB running transpile(sql, write="duckdb"). This script computes, per query, a
fingerprint of everything sqlglot hands to the parts that run the data: the parsed
tree, the optimized tree (types included), the plan, the schema's table args and the
DuckDB SQL. The selector drops the test only if every fingerprint matches the pinned
ones (recorded at image build) AND the patch leaves untouched every function that the
execution side runs (maps/tpcds/exec.json, built from the pinned commit).

    python3 -S tpcds_check.py pin <out>        (Docker build, pinned sources)
    python3 -S tpcds_check.py run < req.json   (selector; {"files": {path: patched source}})
"""
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PINNED = os.path.join(HERE, "pinned")
MAPS = os.path.join(HERE, "maps", "tpcds")


class _Patched(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, files):
        self.files = files

    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        rel = name.replace(".", "/")
        for cand, pkg in ((rel + "/__init__.py", True), (rel + ".py", False)):
            if cand in self.files:
                spec = importlib.machinery.ModuleSpec(name, self, origin=os.path.join(PINNED, cand), is_package=pkg)
                if pkg:
                    spec.submodule_search_locations = [os.path.join(PINNED, rel)]
                spec.has_location = True
                return spec
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        rel = module.__spec__.origin[len(PINNED) + 1:]
        module.__file__ = module.__spec__.origin
        exec(compile(self.files[rel], module.__file__, "exec"), module.__dict__)


def fingerprints():
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from sqlglot.serde import dump

    def enc(v):
        from sqlglot import exp

        if isinstance(v, exp.Expr):
            return {"expr": dump(v)}
        if isinstance(v, (list, tuple)):
            return [enc(x) for x in v]
        if isinstance(v, dict):
            return sorted(([enc(k), enc(x)] for k, x in v.items()), key=repr)
        if isinstance(v, (set, frozenset)):
            return sorted((enc(x) for x in v), key=repr)
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, type):
            return f"type:{v.__module__}.{v.__qualname__}"
        return f"{type(v).__module__}.{type(v).__qualname__}:{v!r}"

    def plan_fp(plan):
        """Order-independent: each step hashes its own fields plus the sorted hashes of
        its dependencies (steps can share a name, so no ordering by name is used)."""
        memo = {}

        def step_fp(st):
            if id(st) in memo:
                return memo[id(st)]
            d = {"type": type(st).__qualname__}
            for k, v in sorted(vars(st).items()):
                if k == "dependents":
                    continue
                d[k] = sorted(step_fp(x) for x in v) if k == "dependencies" else enc(v)
            h = hashlib.blake2b(json.dumps(d, sort_keys=True, default=str).encode(), digest_size=16).hexdigest()
            memo[id(st)] = h
            return h

        return step_fp(plan.root)

    schema_dict = json.load(open(os.path.join(MAPS, "schema.json")))
    out = []
    for execute, sql in json.load(open(os.path.join(MAPS, "queries.json"))):
        rec = []
        try:
            e = parse_one(sql)
            rec.append(dump(e))
            if execute:
                rec.append(sorted(t.name for t in find_tables(e)))
                schema = ensure_schema(schema_dict)
                opt = optimize(e, schema, leave_tables_isolated=True)
                rec.append(dump(opt))
                rec.append(plan_fp(Plan(opt)))
                rec.append(enc(schema.supported_table_args))
                rec.append(transpile(sql, write="duckdb")[0])
        except BaseException as ex:  # noqa: BLE001
            rec.append("error:" + type(ex).__qualname__ + ":" + str(ex)[:200])
        out.append(hashlib.blake2b(json.dumps(rec, sort_keys=True, default=str).encode(), digest_size=16).hexdigest())
    return out


def main():
    sys.path[:0] = [PINNED]
    mode = sys.argv[1]
    if mode == "run":
        sys.meta_path.insert(0, _Patched(json.load(sys.stdin)["files"]))
    fps = fingerprints()
    if mode == "pin":
        json.dump(fps, open(sys.argv[2], "w"))
    else:
        pinned = json.load(open(os.path.join(MAPS, "pinned.json")))
        print(json.dumps({"same": fps == pinned, "differ": [i for i, (a, b) in enumerate(zip(fps, pinned)) if a != b][:10]}))


if __name__ == "__main__":
    main()
