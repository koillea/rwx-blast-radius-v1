"""Blast Radius selector for sqlglot.

For each patch: find the lines it touches (as line numbers in the pinned commit),
map each line to the function that contains it, and run only the tests that executed
that function when the maps were built. Lines outside any function (class or module
level, e.g. a dialect's class attribute) select every test that executed the file or
used a class defined in it. Anything the maps cannot vouch for runs every test.
Then the chosen tests are split over the worker count that minimises
busiest-worker time + 2 s per worker.

Inputs:  $DATA_DIR/commits/<id>.patch, $DATA_DIR/tests.json   (default /data)
Maps:    maps/maps.json, built from the pinned commit by build/ (see README.md)
Output:  one JSON line per commit on stdout; logs on stderr.
"""
from __future__ import annotations

import json
import os
import re
import sys
from bisect import bisect_right
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
MAPS = Path(__file__).resolve().parent / "maps" / "maps.json"
MAX_WORKERS = 8
WORKER_MS = 2000
VERBOSE = os.environ.get("BR_VERBOSE") == "1"


def log(*a):
    if VERBOSE:
        print(*a, file=sys.stderr)


# ------------------------------------------------------------------ patches
_DIFF = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileChange:
    __slots__ = ("old", "new", "lines", "added", "deleted", "blocks")

    def __init__(self, old, new):
        self.old, self.new = old, new
        self.lines: set[int] = set()  # line numbers in the pinned (old) file
        self.added = False
        self.deleted = False
        # contiguous runs of -/+ lines: [first_old_line, removed_texts, added_texts]
        self.blocks: list[list] = []


def parse_patch(text: str) -> list[FileChange]:
    """Collect, per file, the pinned-commit line numbers a patch touches.

    Hunk bodies are consumed by their declared line counts, so a removed line whose
    content starts with "-- " (shown as "--- ...") is never mistaken for a header.
    """
    changes: list[FileChange] = []
    cur: FileChange | None = None
    old = rem_old = rem_new = 0
    blk = None
    lines = text.split("\n")
    for line in lines:
        if rem_old > 0 or rem_new > 0:
            c = line[:1]
            if c == " " or line == "":
                old += 1
                rem_old -= 1
                rem_new -= 1
                blk = None
            elif c == "-":
                if blk is None:
                    blk = [old, [], []]
                    cur.blocks.append(blk)
                blk[1].append(line[1:])
                cur.lines.add(old)
                old += 1
                rem_old -= 1
            elif c == "+":
                if blk is None:
                    blk = [old, [], []]
                    cur.blocks.append(blk)
                blk[2].append(line[1:])
                cur.lines.add(old - 1)
                cur.lines.add(old)
                rem_new -= 1
            # "\ No newline at end of file" consumes nothing
            continue
        blk = None
        m = _DIFF.match(line)
        if m:
            cur = FileChange(m.group(1), m.group(2))
            changes.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode") or line == "--- /dev/null":
            cur.added = True
        elif line.startswith("deleted file mode") or line == "+++ /dev/null":
            cur.deleted = True
        elif line.startswith("rename from") or line.startswith("rename to"):
            cur.deleted = True  # treat a rename as "cannot vouch"
        else:
            h = _HUNK.match(line)
            if h:
                old = int(h.group(1))
                rem_old = int(h.group(2)) if h.group(2) is not None else 1
                rem_new = int(h.group(4)) if h.group(4) is not None else 1
    return changes



# ------------------------------------------------------------------ pinned source
PINNED = Path(__file__).resolve().parent / "maps" / "pinned_src.zip"
_zip = None
_src_cache: dict[str, str | None] = {}
_strings_cache: dict[str, list] = {}


def pinned_source(path: str) -> str | None:
    global _zip
    if path not in _src_cache:
        if _zip is None:
            import zipfile

            _zip = zipfile.ZipFile(PINNED)
        try:
            _src_cache[path] = _zip.read(path).decode("utf-8")
        except KeyError:
            _src_cache[path] = None
    return _src_cache[path]


def string_spans(path: str, src: str) -> list:
    """(first, last) line of every string literal spanning more than one line."""
    if path not in _strings_cache:
        import io
        import tokenize

        spans = []
        fstart = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                name = tokenize.tok_name.get(tok.type, "")
                if name == "STRING" and tok.end[0] > tok.start[0]:
                    spans.append((tok.start[0], tok.end[0]))
                elif name == "FSTRING_START":
                    fstart.append(tok.start[0])
                elif name == "FSTRING_END" and fstart:
                    a = fstart.pop()
                    if tok.end[0] > a:
                        spans.append((a, tok.end[0]))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            spans = None
        _strings_cache[path] = spans
    return _strings_cache[path]


def _is_comment_or_blank(text: str) -> bool:
    t = text.strip()
    return not t or (t.startswith("#") and "coding" not in t[:40])


def block_is_noop(path: str, src_lines: list[str], spans, blk) -> bool:
    """True when a change block only adds/removes full-line comments or blank lines
    outside any string literal and not after a backslash continuation: Python ignores it."""
    first, removed, added = blk
    if spans is None:
        return False
    if not all(_is_comment_or_blank(x) for x in removed + added):
        return False
    prev = first - 1  # last unchanged old line before the block
    if 1 <= prev <= len(src_lines) and src_lines[prev - 1].rstrip().endswith("\\"):
        return False
    last = first + len(removed)  # first unchanged old line after the block
    for a, b in spans:
        if a <= prev and b >= last:  # the block sits inside a multi-line string
            return False
        if removed and a < first + len(removed) and b >= first:
            return False
    return True


def patched_source(src: str, ch: FileChange) -> str:
    lines = src.split("\n")
    out = []
    i = 1
    for first, removed, added in sorted(ch.blocks, key=lambda b: b[0]):
        out.extend(lines[i - 1:first - 1])
        out.extend(added)
        i = first + len(removed)
    out.extend(lines[i - 1:])
    return "\n".join(out)


def significant_lines(ch: FileChange) -> set[int] | None:
    """Old line numbers of the blocks that can change behaviour; None if the
    patched file no longer parses (an import error fails every importer)."""
    path = ch.old
    src = pinned_source(path)
    if src is None:
        return set(ch.lines)
    src_lines = src.split("\n")
    spans = None
    keep: set[int] = set()
    any_real = False
    for blk in ch.blocks:
        first, removed, added = blk
        if all(_is_comment_or_blank(x) for x in removed + added):
            if spans is None:
                spans = string_spans(path, src)
            if block_is_noop(path, src_lines, spans, blk):
                log(f"  {path}:{first} comment/blank only -> ignored")
                continue
        any_real = True
        if removed:
            # a replacement or deletion changes exactly the removed old lines
            keep.update(range(first, first + len(removed)))
        else:
            # a pure insertion sits between old lines first-1 and first
            keep.add(first - 1)
            keep.add(first)
    if any_real:
        import ast

        try:
            ast.parse(patched_source(src, ch))
        except (SyntaxError, ValueError):
            log(f"  {path}: patched file does not parse -> all")
            return None
    return keep


# ------------------------------------------------------------------ split
def split(nums, dur):
    if not nums:
        return []
    order = sorted(nums, key=lambda n: dur[n], reverse=True)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            ws[i].append(n)
            loads[i] += dur[n]
        used = sum(1 for w in ws if w)
        cost = max(loads) + WORKER_MS * used
        if best is None or cost < best[0]:
            best = (cost, [w for w in ws if w])
    return best[1]


# ------------------------------------------------------------------ selection
DOC_EXT = (".md", ".rst", ".txt", ".png", ".svg", ".html")


class Selector:
    def __init__(self, tests):
        m = json.loads(MAPS.read_text())
        self.all = {t["number"] for t in tests}
        self.ids = {t["id"]: t["number"] for t in tests}
        self.by_file: dict[str, set[int]] = {}
        self.by_class: dict[str, set[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
            self.by_class.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])
        self.file_cov = m["file_cov"]
        self.file_use = m["file_use"]
        self.fixtures = m["fixtures"]
        self.spans = {f: sorted(v) for f, v in m["spans"].items()}
        self.units = {(f, s, e): (set(ts), bool(imp)) for f, s, e, ts, imp in m["units"]}
        self.test_spans = m["test_spans"]
        self.cls_lines = {f: {int(k): v for k, v in d.items()} for f, d in m.get("cls_lines", {}).items()}
        self.docstrings = m.get("docstrings", {})
        self._trees: dict[str, object] = {}
        self._all_src: list[tuple[str, list[str]]] | None = None
        fdirs: dict[str, set[int]] = {}
        for fx, ts in self.fixtures.items():
            fdirs.setdefault(os.path.dirname(fx), set()).update(ts)
        self.fixture_dirs = fdirs

    def file_level(self, f):
        cov, use = self.file_cov.get(f), self.file_use.get(f)
        if cov is None and use is None:
            return None
        return set(cov or ()) | set(use or ())

    # -------------------------------------------------------------- class/module level
    def _tree(self, f):
        if f not in self._trees:
            import ast

            src = pinned_source(f)
            try:
                self._trees[f] = ast.parse(src) if src is not None else None
            except SyntaxError:
                self._trees[f] = None
        return self._trees[f]

    def _sources(self):
        if self._all_src is None:
            pinned_source("sqlglot/__init__.py")  # opens the zip
            out = []
            for name in _zip.namelist():
                if name.endswith(".py"):
                    out.append((name, _zip.read(name).decode("utf-8", "replace").split("\n")))
            self._all_src = out
        return self._all_src

    def _in_docstring(self, f, line):
        return any(a <= line <= b for a, b in self.docstrings.get(f, ()))

    def _unit_for(self, f, line):
        inner = None
        for s0, e, b in self.spans.get(f, ()):
            if s0 <= line <= e and (inner is None or e - s0 < inner[1] - inner[0]):
                inner = (s0, e, b)
        return inner

    def class_level(self, f, line, ch):
        """Tests that can observe a class/module-level edit at `line`, or None to fall
        back to the whole file. Two cases are narrowed:
          * an edit inside a lambda body: only calls of the lambda see it, and those
            executed the line at run time;
          * an UPPERCASE constant assigned a call-free value: only code that reads the
            name sees it, so take the tests that ran a function reading it. A read at
            import time (class bodies, metaclasses) falls back to the whole file.
        """
        import ast

        tree = self._tree(f)
        if tree is None:
            return None
        changed = {l for l in ch.lines}
        # ---- lambda bodies
        lam = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Lambda):
                b = node.body
                if b.lineno < line < b.end_lineno or (b.lineno <= line <= b.end_lineno and b.lineno != b.end_lineno and line not in (b.lineno, b.end_lineno)):
                    if lam is None or (b.end_lineno - b.lineno) < (lam.end_lineno - lam.lineno):
                        lam = b
        if lam is not None and all(lam.lineno < l < lam.end_lineno for l in changed if abs(l - line) <= 1):
            return set(self.cls_lines.get(f, {}).get(line, ()))
        # ---- simple constant assignment
        stmt = None

        def visit(body):
            nonlocal stmt
            for n in body:
                if n.lineno <= line <= n.end_lineno:
                    if isinstance(n, ast.ClassDef):
                        visit(n.body)
                    elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        stmt = n

        visit(tree.body)
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or stmt.value is None:
            return None
        if any(isinstance(x, (ast.Call, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Await))
               for x in ast.walk(stmt.value)):
            return None
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        names = []
        for t in targets:
            if not isinstance(t, ast.Name) or not t.id.isupper() or len(t.id) < 4:
                return None
            names.append(t.id)
        sel: set[int] = set()
        for name in names:
            word = re.compile(r"\b" + re.escape(name) + r"\b")
            define = re.compile(r"^\s*" + re.escape(name) + r"\s*(:[^=]|=(?!=))")
            for g, glines in self._sources():
                for i, text in enumerate(glines, 1):
                    if name not in text or not word.search(text):
                        continue
                    t = text.strip()
                    if t.startswith("#") or define.match(text) or self._in_docstring(g, i):
                        continue
                    if g.startswith("tests/"):
                        r = self._test_line(g, i)
                        if r is None:
                            return None
                        sel |= r
                        continue
                    u = self._unit_for(g, i)
                    if u is None or i < u[2]:
                        log(f"    read of {name} at import time {g}:{i} -> file")
                        return None
                    ts, imp = self.units.get((g, u[0], u[1]), (set(), False))
                    if imp:
                        return None
                    sel |= ts
        log(f"  {f}:{line} constant {names} -> readers {len(sel)}")
        return sel

    def _test_line(self, f, line):
        spans = self.test_spans.get(f)
        if spans is None:
            return None
        inner = None
        for s0, e, q in spans:
            if s0 <= line <= e and (inner is None or e - s0 < inner[1] - inner[0]):
                inner = (s0, e, q)
        tid = f + "::" + inner[2].replace(".", "::") if inner else None
        if tid in self.ids:
            return {self.ids[tid]}
        return set(self.by_file.get(f, ())) or None

    def source(self, ch: FileChange):
        f = ch.old
        sel: set[int] = set()
        spans = self.spans.get(f)
        if spans is None:
            log(f"  {f}: unknown source file -> all")
            return None
        for line in sorted(ch.lines):
            inner = None
            for s, e, b in spans:
                if s <= line <= e and (inner is None or e - s < inner[1] - inner[0]):
                    inner = (s, e, b)
            if inner is None:
                narrowed = self.class_level(f, line, ch)
                if narrowed is not None:
                    sel |= narrowed
                    continue
                fl = self.file_level(f)
                if fl is None:
                    return None
                log(f"  {f}:{line} module/class level -> {len(fl)}")
                sel |= fl
                continue
            ts, imp = self.units.get((f, inner[0], inner[1]), (set(), False))
            sel |= ts
            if imp:
                fl = self.file_level(f)
                if fl is None:
                    return None
                sel |= fl
            log(f"  {f}:{line} in fn {inner[:2]} -> {len(ts)}{' +file' if imp else ''}")
        return sel

    def test_file(self, ch: FileChange):
        f = ch.old
        spans = self.test_spans.get(f)
        if spans is None:
            return None
        sel: set[int] = set()
        for line in ch.lines:
            inner = None
            for s, e, q in spans:
                if s <= line <= e and (inner is None or e - s < inner[1] - inner[0]):
                    inner = (s, e, q)
            tid = f + "::" + inner[2].replace(".", "::") if inner else None
            if tid in self.ids:
                sel.add(self.ids[tid])
            else:
                return None  # helper or module level in a test file: may be shared
        return sel

    def fixture(self, ch: FileChange):
        f = ch.old if not ch.added else ch.new
        if f in self.fixtures:
            return set(self.fixtures[f])
        d = os.path.dirname(f)
        if d in self.fixture_dirs:
            log(f"  {f}: unread fixture -> its directory's readers")
            return set(self.fixture_dirs[d])
        return None

    def select(self, changes: list[FileChange]) -> set[int]:
        sel: set[int] = set()
        for ch in changes:
            path = ch.new if ch.added else ch.old
            log(f" {path} lines={sorted(ch.lines)[:12]} added={ch.added} deleted={ch.deleted}")
            if path.endswith(DOC_EXT) or path.startswith(("docs/", "posts/", "pdoc/", ".github/")):
                continue
            if path.startswith("tests/fixtures/"):
                r = self.fixture(ch) if not ch.deleted else None
            elif ch.added or ch.deleted:
                r = None
            elif path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
                sig = significant_lines(ch)
                if sig is None:
                    r = None
                else:
                    ch.lines = sig
                    r = self.source(ch) if path.startswith("sqlglot/") else self.test_file(ch)
            else:
                r = None
            if r is None:
                log(f"  {path}: cannot vouch -> all tests")
                return set(self.all)
            sel |= r
        return sel


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    sel = Selector(tests)
    out = []
    for p in sorted((DATA / "commits").glob("*.patch")):
        log(p.stem)
        chosen = sel.select(parse_patch(p.read_text(encoding="utf-8", errors="replace")))
        out.append(json.dumps({"commit": p.stem, "workers": split(sorted(chosen), dur)}))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
