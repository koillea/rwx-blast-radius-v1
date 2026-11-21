# RWX Blast Radius — test selection for sqlglot (team Goonies)

`Dockerfile`, `selector.py` and `maps/maps.json` at the top of this repository are our entry for the RWX **Blast Radius** track: for each sqlglot patch, choose which tests to run and split them over up to 8 workers, minimising simulated CI seconds (busiest worker + 2 s per worker + 300 s per missed failing commit + CPU time).

**How it decides** (`selector.py`):
1. Parse the patch into the pinned-commit line numbers it touches.
2. Map each line to its innermost function. Run only the tests that executed the **statement** containing the edit when the maps were built; for an `if`/`for`, that means its header lines. The selector falls back to every test that executed the function for:
   - pure insertions;
   - statements no test executed;
   - edits that change the names the function binds, or its `yield`/`await`/`global`/`nonlocal`. A change that a test never reaches cannot alter that test's run.
3. Lines outside any function (class attributes such as a dialect's `WEEK_OFFSET`, module tables) select every test that executed the file *or* instantiated a class defined there or in its bases. Code that runs only at import time selects the whole file's tests.
4. Comment-only or blank-line edits are ignored, because Python never sees them. The check is made against the pinned source shipped in `maps/pinned_src.zip`, and an edit doesn't count as comment-only if it sits inside a multi-line string or after a `\` continuation. Every patched `.py` file is parsed; if it no longer parses, every test runs, since an import error fails every importer.
5. Class- and module-level edits are narrowed where that's provably safe:
   - **An edit inside a `lambda` body** (for example an entry of a parser's `FUNCTIONS` table) runs the tests that called it.
   - **A class-level constant dict whose edit changes only VALUES (same keys)** runs only the tests that read a changed key's value, or used the dict as a whole, in that class's dict or a subclass's copy (`build/dictprobe.py`, `maps/dicts.bin.z`: each such dict is wrapped right after its module is imported, recording per test which keys' values are read; reads during later imports count for every importer). A changed key must match exactly one real key of the dict; import-time readers in modules loaded before recording must be copies or key-only checks; otherwise the rules below apply.
   - **Any class-level UPPERCASE constant** (`NAME = ...` in class C, still assigning NAME) runs only the tests whose reads of NAME resolved to C's definition (directly or through a subclass that does not override it), recorded by `build/attrprobe.py` (`maps/attrs.bin.z`) for every `X.NAME` load, imports included; where NAME is passed unchanged into a constructor that stores it, the later uses of the stored attribute count instead. Names looked up by a quoted string, or used bare in a class body, fall back to the rules below.
   - **An UPPERCASE constant with a call-free value** (such as `WEEK_OFFSET`) runs the tests that executed a function reading that name; where that would add a slow test, only the tests that executed the reading statement itself. If any read happens at import time (class bodies, metaclasses), the whole file's tests run instead.
   - **An edit confined to one `lambda` body**, one-line lambdas included (checked by comparing the old and new syntax trees), runs only the tests that executed a line of that body, plus importers of any import that called it.
   - **An edit to an expression class's `arg_types`, `is_var_len_args`, `is_primitive` or `_hash_raw_args`** (attributes sqlglot reads only through an instance, or in `from_arg_list` just before instantiating) runs the tests that created that class or a subclass (every read in sqlglot goes through an instance), the code that spells out `C.arg_types`/`C.required_args`, and tests that name the class. A copy made while another expression class is defined (`TryCast` copying `Cast`'s) adds that class's observers.
6. **Docstring, comment and layout edits:** if the patched file has the same syntax tree as the pinned one once every bare string statement is blanked (docstrings, and the attribute docstrings after assignments, which the compiler discards), nothing runs. No collected test reads a docstring. The exception is `tests/test_typing.py`, which compares annotator code objects, so it always runs for edits under `sqlglot/typing/`.
7. **Comparison swaps:** an edit whose only effect is to swap one comparison for its partner (`is`↔`==`, `is not`↔`!=`, `<`↔`<=`, `>`↔`>=`) can change behaviour only where the two operators disagree on the operands actually seen. A second build pass (`build/cmpprobe.py`) evaluates both operators at every such comparison in sqlglot and records the tests in which they ever disagreed (`maps/cmp.json`). If evaluating the partner operator runs the same comparison again (e.g. a swap inside `__eq__` itself, which would recurse once applied), that evaluation counts as disagreeing. Only those tests run. If they disagreed while sqlglot was being imported, the normal rules apply.
8. **Imports.** A module's import can fail, and then every test that imports it fails, including tests that never run the edited code. For example, the DuckDB generator parses SQL while it is defined, so a dialect flag can break `import`. While the maps are built, every first import of a sqlglot or test module runs under its own coverage context, and every import request is recorded, including requests for modules that are already loaded. Code run during the import of module Y therefore counts for Y's importers: every test whose file imports Y, directly or through other modules, plus every test that imports Y while running. A module- or class-level edit adds those importers in two cases:
   - the new statement may raise. Only literals, containers and names it already used are safe;
   - code that mentions a name it assigns ran during Y's import, and that edit's class was used during Y's import (or the line names the class).

   Edits confined to a lambda body skip the name check: only calls of that lambda see them, and calls during imports are counted. A function header (decorators, defaults) is checked the same way, because it runs when the `def` runs.
9. Every narrowing of a class-level edit is intersected with the file-level set. Both are sound over-approximations, so a narrowing rule can only shrink a selection. "Import time" means only code run while sqlglot itself is imported; code a test module runs while it is imported counts for that module's tests only.
10. Fixture edits run the tests that opened that fixture. Test-method edits run that test (and the same method in subclasses, in files importing it); helper or module-level edits in a test file run the tests of every file that imports it. Repository files no test reads (Makefile, .gitignore, LICENSE, MANIFEST.in, benchmarks/, ...) select nothing; sqlglotc/ selects tests/test_sqlglotc.py. Added and deleted files: a deleted module runs every test that imported it (import graph) plus its users; a deleted test file runs its tests and its importers' tests; a deleted fixture runs its readers; an added sqlglot or test module runs nothing (nothing lists directories except test_typing for sqlglot/typing/), except a new package that shadows a module, a conftest.py/__init__.py in tests, or any new root file. Anything else the maps cannot vouch for (setup.py, pyproject.toml, setup.cfg, unknown files) runs every test, because a run-all (~37 s) is far cheaper than a miss (300 s).
11. Split the chosen tests longest-first over the worker count (1–8) that minimises busiest-worker time + 2 s per worker.

**The maps** (`build/`, stored as zlib-compressed `marshal` with each distinct test set kept once and built only when used) are built once from sqlglot at the pinned commit `eb11aec`, and committed. They hold:
- per-test coverage, recorded with each test's own coverage context;
- the sqlglot classes each test instantiates, with their bases;
- the fixture files each test opens.

The suite's process pools run in-process during the build, with every task pickled and unpickled as a real process boundary would, so work done "in a worker" is credited to its test and `serde` (sqlglot's pickling) counts as used. Nothing is derived from any patch. Rebuild with `bash build/rebuild_maps.sh <tests.json>`.

**Local results on the 30 sample commits** (the kit's `score.py`; the board uses different, hidden commits and adds CPU time, about 0.4 s for this selector):

| Version | Simulated seconds | Misses |
|---|---|---|
| v3 | 185.452 | 0 |
| v4 | 179.454 | 0 |
| v6 | 179.476 | 0 |
| v7 (comparison swaps) | 126.699 | 0 |
| v8 (same picks as v7, less CPU) | 126.699 | 0 |
| v9 (`arg_types` reads) | 119.652 | 0 |
| v10 (import exposure: closes a miss v3–v9 shared) | 119.652 | 0 |
| v11 (same picks as v10, ~43% less CPU) | 119.652 | 0 |
| v12 (attribute docstrings; more instance-read attributes) | 119.652 | 0 |
| v13 (constant readers narrowed to the reading statement) | 119.652 | 0 |
| v14 (one-line lambdas, e.g. generator TRANSFORMS) | 119.652 | 0 |
| v15 (non-source files, test-file helpers) | 119.652 | 0 |
| v16 (added and deleted files) | 119.652 | 0 |
