"""Build maps/tpcds/exec.json: the sqlglot lines the EXECUTION side of
test_execute_tpcds runs on the pinned commit (table setup, pickling expressions and
tables to workers, find_tables, ensure_tables, the Python executor and its code
generation), as opposed to the parts tpcds_check.py re-runs and compares (parse_one,
ensure_schema, optimize, Plan, transpile to DuckDB).

usage (from a pinned sqlglot checkout):  python build/tpcds_exec.py <out_dir>
It reproduces the sqlglot calls of TestExecutor.setUpClass and test_execute_tpcds in
one process, switching coverage contexts around the compared calls.
"""
import ast
import csv
import gzip
import os
import pickle
import sys

import coverage

OUT = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.getcwd())  # the pinned checkout, not an installed sqlglot
DATA_FILE = os.path.join(OUT, ".tpcds_exec.cov")
cov = coverage.Coverage(source=[os.path.abspath("sqlglot")], data_file=DATA_FILE)
cov.start()
cov.switch_context("exec")

import sqlglot.executor as ex  # noqa: E402
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
from sqlglot.executor.table import Table  # noqa: E402

from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs  # noqa: E402


def compared(fn):
    def wrapper(*a, **kw):
        cov.switch_context("pre")
        try:
            return fn(*a, **kw)
        finally:
            cov.switch_context("exec")
    return wrapper


ex.optimize = compared(ex.optimize)
ex.Plan = compared(ex.Plan)
ex.ensure_schema = compared(ex.ensure_schema)
c_parse, c_transpile = compared(parse_one), compared(transpile)

DIR = "tests/fixtures/optimizer/tpc-ds/"
tables = {}
for table, columns in TPCDS_SCHEMA.items():  # setUpClass's setup(), sqlglot side
    rows, ctypes = [], []
    reader = csv.reader(gzip.open(f"{DIR}{table}.csv.gz", "rt"), delimiter="|")
    next(reader)
    for row in reader:
        if not ctypes:
            for v in row:
                try:
                    ctypes.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    tables[table] = Table(columns=columns, rows=rows)
tables = pickle.loads(pickle.dumps(tables))  # initargs to the worker processes

n = 0
for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    e = pickle.loads(pickle.dumps(c_parse(sql)))  # submitted to a worker
    if meta.get("execute"):
        tbls = {t.name: tables[t.name] for t in find_tables(e)}
        result = ex.execute(e, schema=TPCDS_SCHEMA, tables=tbls)
        pickle.loads(pickle.dumps(result))  # the result comes back from the worker
        _ = [list(r) for r in result.rows], list(result.columns)
        c_transpile(sql, write="duckdb")
        n += 1

cov.stop()
cov.save()
data = coverage.CoverageData(DATA_FILE)
data.read()
out = {}
root = os.path.abspath(".") + os.sep
for f in data.measured_files():
    lines = set()
    for ln, ctxs in (data.contexts_by_lineno(f) or {}).items():
        if "exec" in ctxs:
            lines.add(ln)
    if lines:
        out[os.path.relpath(f, root)] = sorted(lines)
import json  # noqa: E402

os.makedirs(OUT, exist_ok=True)
json.dump(out, open(os.path.join(OUT, "exec.json"), "w"))
os.remove(DATA_FILE)
print("executed queries", n, "files with execution-side lines", len(out), "lines", sum(len(v) for v in out.values()))
