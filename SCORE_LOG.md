# Score log

Kit sample = the kit's 30 commits, scored with the kit's `score.py` (same simulated seconds
as `make sample`). CPU is the selector's user+system time on our build machine (median of
5 runs); the board charges about 2x this. Board = the official practice board (hidden
commits; simulated + CPU, middle of 3 runs).

Simulated = test seconds (busiest worker per commit) + worker seconds (2 s per worker)
+ 300 s per miss.

| Version | Commit | Kit total | Kit simulated | Test s | Worker s | Misses | CPU | Board | What changed |
|---|---|---|---|---|---|---|---|---|---|
| v3 | `11ff728` | 185.8 | 185.452 | 117.452 | 68 | 0 | 0.34 | **257.0** (256.42 + 0.57) | Coverage map per test (function level), class usage, fixture map, LPT split |
| v4 | `091b272` | 179.9 | 179.454 | 113.454 | 66 | 0 | 0.45 | - | Statement-level selection inside functions; class-level narrowing intersected with file level |
| v5 | `14a0960` | 180.1 | 179.454 | 113.454 | 66 | 0 | 0.64 | - | Edits confined to one lambda body (AST-checked) |
| v6 | `6532704` | 180.5 | 179.476 | 113.476 | 66 | 0 | 1.01 | - | Docstring/layout-only edits select nothing; test_typing always for typing/ |
| v7 | `96c71b8` | 127.8 | 126.699 | 68.699 | 58 | 0 | 1.07 | **208.3** (206.15 + 2.17) | Comparison swaps (is/==, </<=, ...) select only tests where the operators ever disagreed (probe built at map time) |
| v8 | `25c06f7` | 127.3 | 126.699 | 68.699 | 58 | 0 | 0.60 | - | Same picks, less CPU |
| v9 | `832535b` | 120.4 | 119.652 | 63.652 | 56 | 0 | 0.73 | - | `arg_types` edits: only tests that can read that class's arg_types |
| v10 | `25babc4` | 120.3 | 119.652 | 63.652 | 56 | 0 | 0.68 | - | Import contexts: code run during an import counts for every importer (closes a miss v3-v9 shared) |
| v11 | `0c974ed` | 120.0 | 119.652 | 63.652 | 56 | 0 | 0.34 | **200.8** (200.14 + 0.61) | Same picks, ~43% less CPU (marshal maps, lazy sets) |
| v12 | `db8a029` | 120.0 | 119.652 | 63.652 | 56 | 0 | 0.38 | - | Discarded string statements are no-ops; instance-read rule for more expression attributes |
| v13 | `668ce0b` | 120.1 | 119.652 | 63.652 | 56 | 0 | 0.46 | - | Constant readers narrowed to the reading statement |
| v14 | `edd4829` | 120.0 | 119.652 | 63.652 | 56 | 0 | 0.36 | **200.9** (200.14 + 0.71) | One-line lambdas (generator TRANSFORMS) |
| v15 | `HASH` | 120.0 | 119.652 | 63.652 | 56 | 0 | 0.37 | - | Non-source files no test reads select nothing; sqlglotc/ -> test_sqlglotc; test-file helpers -> importers of that file, not all tests |

Reference: a perfect selector (the answers) scores 94.4 s simulated on the kit sample.
Board model so far: board simulated ~= 97.9 + 0.855 x kit simulated (fits v3, v7, v11 within 0.1 s).
