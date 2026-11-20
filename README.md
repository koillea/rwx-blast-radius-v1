# RWX Blast Radius — test selection for sqlglot (team Goonies)

`Dockerfile`, `selector.py` and `maps/maps.json` at the top of this repository are our entry for the RWX **Blast Radius** track: for each sqlglot patch, choose which tests to run and split them over up to 8 workers, minimising simulated CI seconds (busiest worker + 2 s per worker + 300 s per missed failing commit + CPU time).

**How it decides** (`selector.py`):
1. Parse the patch into the pinned-commit line numbers it touches.
2. Map each line to its innermost function. Run only the tests that executed that function's body when the maps were built. A change that a test never reaches cannot alter that test's run.
3. Lines outside any function (class attributes such as a dialect's `WEEK_OFFSET`, module tables) select every test that executed the file *or* instantiated a class defined there or in its bases. Code that runs only at import time selects the whole file's tests.
4. Comment-only or blank-line edits are ignored, because Python never sees them. The check is made against the pinned source shipped in `maps/pinned_src.zip`, and an edit doesn't count as comment-only if it sits inside a multi-line string or after a `\` continuation. Every patched `.py` file is parsed; if it no longer parses, every test runs, since an import error fails every importer.
5. Class- and module-level edits are narrowed where that's provably safe:
   - **An edit inside a `lambda` body** (for example an entry of a parser's `FUNCTIONS` table) runs the tests that called it.
   - **An UPPERCASE constant with a call-free value** (such as `WEEK_OFFSET`) runs the tests that executed a function reading that name. If any read happens at import time (class bodies, metaclasses), the whole file's tests run instead.
6. Fixture edits run the tests that opened that fixture. Test-method edits run that test. Anything the maps cannot vouch for (new or deleted files, test helpers, unknown files) runs every test, because a run-all (~37 s) is far cheaper than a miss (300 s).
7. Split the chosen tests longest-first over the worker count (1–8) that minimises busiest-worker time + 2 s per worker.

**The maps** (`build/`) are built once from sqlglot at the pinned commit `eb11aec`, and committed. They hold:
- per-test coverage, recorded with each test's own coverage context;
- the sqlglot classes each test instantiates, with their bases;
- the fixture files each test opens.

The suite's process pools run in-process during the build, with every task pickled and unpickled as a real process boundary would, so work done "in a worker" is credited to its test and `serde` (sqlglot's pickling) counts as used. Nothing is derived from any patch. Rebuild with `bash build/rebuild_maps.sh <tests.json>`.

**Local results on the 30 sample commits:**

| Selector | Simulated seconds | Misses |
|
