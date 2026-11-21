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
MAPS = Path(__file__).resolve().parent / "maps" / "maps.bin.z"
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


def _cmp_partner():
    import ast

    return {ast.Is: (ast.Eq, "is"), ast.Eq: (ast.Is, "=="), ast.IsNot: (ast.NotEq, "is not"),
            ast.NotEq: (ast.IsNot, "!="), ast.Lt: (ast.LtE, "<"), ast.LtE: (ast.Lt, "<="),
            ast.Gt: (ast.GtE, ">"), ast.GtE: (ast.Gt, ">=")}




def same_code(ch: FileChange) -> bool:
    """True if the patched file parses to the same AST as the pinned one once
    docstrings are blanked: only comments, layout or docstring text changed. No test
    in the suite reads a docstring (the doctest module is not collected by pytest)."""
    import ast

    src = pinned_source(ch.old)
    if src is None:
        return False
    try:
        trees = [ast.parse(src), ast.parse(patched_source(src, ch))]
    except SyntaxError:
        return False
    for tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
                d = n.body[0]
                if isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant) and isinstance(d.value.value, str):
                    d.value = ast.Constant("")
    return ast.dump(trees[0]) == ast.dump(trees[1])


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


class _Sets:
    """The maps' distinct test sets, turned into frozensets only when first used."""

    def __init__(self, raw):
        self.raw, self.done = raw, {}

    def __getitem__(self, i):
        v = self.done.get(i)
        if v is None:
            v = self.done[i] = frozenset(self.raw[i])
        return v


class _Lazy(dict):
    """key -> set index, read as key -> frozenset."""

    def __init__(self, d, sets):
        super().__init__(d)
        self._s = sets

    def __getitem__(self, k):
        return self._s[dict.__getitem__(self, k)]

    def get(self, k, default=None):
        return self._s[dict.__getitem__(self, k)] if dict.__contains__(self, k) else default

    def items(self):
        return ((k, self._s[v]) for k, v in dict.items(self))

    def values(self):
        return (self._s[v] for v in dict.values(self))


class _LazyUnits(dict):
    def __init__(self, d, sets):
        super().__init__(d)
        self._s = sets

    def get(self, k, default=None):
        if not dict.__contains__(self, k):
            return default
        i, imp = dict.__getitem__(self, k)
        return self._s[i], bool(imp)

    __getitem__ = lambda self, k: self.get(k)


class _LazyList:
    def __init__(self, raw, sets):
        self.raw, self._s = raw, sets

    def __getitem__(self, i):
        return self._s[self.raw[i]]

    def __len__(self):
        return len(self.raw)


class Selector:
    def __init__(self, tests):
        import marshal
        import zlib

        m = marshal.loads(zlib.decompress(MAPS.read_bytes()))
        S = _Sets(m["sets"])  # each distinct test set, stored once, built on first use
        self._cmp = None
        self.all = {t["number"] for t in tests}
        self.ids = {t["id"]: t["number"] for t in tests}
        self.by_file: dict[str, set[int]] = {}
        self.by_class: dict[str, set[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
            self.by_class.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])
        self.file_cov = _Lazy(m["file_cov"], S)
        self.file_use = _Lazy(m["file_use"], S)
        self.fixtures = _Lazy(m["fixtures"], S)
        self.spans = {f: sorted(v) for f, v in m["spans"].items()}
        self.units = _LazyUnits({(f, s, e): (ts, imp) for f, s, e, ts, imp in m["units"]}, S)
        self.test_spans = m["test_spans"]
        self.cls_lines = {f: _Lazy({int(k): v for k, v in d.items()}, S) for f, d in m.get("cls_lines", {}).items()}
        self.docstrings = m.get("docstrings", {})
        self.class_use = _Lazy(m.get("class_use", {}), S)
        # import-time data: which module imports ran each line, which imports used or
        # defined each class, and each imported module's importers (tests that fail
        # if importing it fails)
        self.imp_importers = _LazyList(m.get("imp_importers", []), S)
        self.imp_line_mods = {f: {int(k): v for k, v in d.items()} for f, d in m.get("imp_line_mods", {}).items()}
        self.touched = m.get("touched", {})
        self._trees: dict[str, object] = {}
        self._all_src: list[tuple[str, list[str]]] | None = None
        self._lines = None
        fdirs: dict[str, set[int]] = {}
        for fx, ts in self.fixtures.items():
            fdirs.setdefault(os.path.dirname(fx), set()).update(ts)
        self.fixture_dirs = fdirs

    def file_level(self, f):
        cov, use = self.file_cov.get(f), self.file_use.get(f)
        if cov is None and use is None:
            # a known module that only ran while being imported: no runtime observers
            # (what an import can expose is added by import_exposure/header_exposure)
            return set() if (f in self.imp_line_mods or f in self.spans) else None
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
                    text = _zip.read(name).decode("utf-8", "replace")
                    out.append((name, text.split("\n"), text))
            self._all_src = out
        return self._all_src

    def _sources_with(self, needle):
        """(file, lines) of the sources whose text contains `needle` at all."""
        return [(g, gl) for g, gl, text in self._sources() if needle in text]

    def _in_docstring(self, f, line):
        return any(a <= line <= b for a, b in self.docstrings.get(f, ()))

    def _unit_for(self, f, line):
        inner = None
        for s0, e, b in self.spans.get(f, ()):
            if s0 <= line <= e and (inner is None or e - s0 < inner[1] - inner[0]):
                inner = (s0, e, b)
        return inner

    def _lambda_confined(self, f, ch, lam) -> bool:
        """True if old and patched file differ only inside the body of `lam`."""
        import ast

        if any(not (lam.body.lineno <= l <= lam.body.end_lineno) for l in ch.lines):
            return False
        if any(first <= lam.lineno for first, _r, _a in ch.blocks):
            return False  # the lambda itself must sit before every change
        src = pinned_source(f)
        try:
            old_t, new_t = ast.parse(src), ast.parse(patched_source(src, ch))
        except SyntaxError:
            return False

        def blank(tree):
            hit = 0
            for n in ast.walk(tree):
                if isinstance(n, ast.Lambda) and (n.lineno, n.col_offset) == (lam.lineno, lam.col_offset):
                    n.body = ast.Constant(None)
                    hit += 1
            return hit == 1

        return blank(old_t) and blank(new_t) and ast.dump(old_t) == ast.dump(new_t)

    def expression_arg_types(self, f, stmt, owner):
        """An edit to `arg_types` of an expression class C is visible only through:
          * instances of C or of a subclass that inherits it (every read in sqlglot
            goes through an instance: self/node/expression.arg_types, and the
            required_args that __init_subclass__ derives from it is read the same way;
            from_arg_list reads cls.arg_types and then instantiates cls);
          * code that spells out C.arg_types / C.required_args;
          * tests that name C at all.
        A spelled-out read at import time inside another expression class's own
        arg_types (e.g. TryCast copying Cast's) adds that class's observers; any other
        import-time read selects the whole file holding it."""
        import ast

        if not self.class_use or not f.startswith("sqlglot/expressions/") or len(owner) != 1:
            return None  # without class-usage data this rule cannot be sound
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return None
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if [getattr(t, "id", None) for t in targets] != ["arg_types"]:
            return None
        return self._arg_types_observers(f, owner[0], set())

    def _arg_types_copier(self, g, line):
        """(file, class) if `line` is inside `arg_types = ...` of a top-level class in g."""
        import ast

        if not g.startswith("sqlglot/expressions/"):
            return None
        tree = self._tree(g)
        if tree is None:
            return None
        for n in tree.body:
            if isinstance(n, ast.ClassDef) and n.lineno <= line <= n.end_lineno:
                for b in n.body:
                    if (isinstance(b, ast.Assign) and b.lineno <= line <= b.end_lineno
                            and [getattr(t, "id", None) for t in b.targets] == ["arg_types"]):
                        return n.name
        return None

    def _arg_types_observers(self, f, cname, seen):
        if (f, cname) in seen:
            return set()
        seen.add((f, cname))
        sel = set(self.class_use.get(f"{f}::{cname}", ()))
        word = re.compile(r"\b" + re.escape(cname) + r"\b")
        attr = re.compile(r"\b" + re.escape(cname) + r"\s*\.\s*(?:arg_types|required_args)\b")
        for g, glines in self._sources_with(cname):
            for i, text in enumerate(glines, 1):
                if cname not in text or not word.search(text):
                    continue
                if text.strip().startswith("#") or self._in_docstring(g, i):
                    continue
                if g.startswith("tests/"):
                    r = self._test_line(g, i)
                    if r is None:
                        return None
                    sel |= r
                    continue
                if not attr.search(text):
                    continue  # names C without reading its arg_types
                u = self._unit_for(g, i)
                if u is None or i < u[2]:
                    copier = self._arg_types_copier(g, i)
                    if copier is not None:
                        r = self._arg_types_observers(g, copier, seen)
                        if r is None:
                            return None
                        sel |= r
                        continue
                    fl = self.file_level(g)
                    if fl is None:
                        return None
                    sel |= fl
                    continue
                ts, imp = self.units.get((g, u[0], u[1]), (set(), False))
                if imp:
                    fl = self.file_level(g)
                    if fl is None:
                        return None
                    sel |= fl
                sel |= ts
        log(f"  {f} {cname}.arg_types -> {len(sel)}")
        return sel

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
        # only the statement that holds the line can contain a lambda around it
        holder = None
        node = tree
        while True:
            nxt = None
            for c in getattr(node, "body", ()):
                if c.lineno <= line <= c.end_lineno:
                    nxt = c
                    break
            if nxt is None:
                break
            holder = nxt
            if not isinstance(nxt, ast.ClassDef):
                break
            node = nxt
        local = list(ast.walk(holder)) if holder is not None else []
        # ---- lambda bodies
        lam = None
        for node in local:
            if isinstance(node, ast.Lambda):
                b = node.body
                if b.lineno < line < b.end_lineno or (b.lineno <= line <= b.end_lineno and b.lineno != b.end_lineno and line not in (b.lineno, b.end_lineno)):
                    if lam is None or (b.end_lineno - b.lineno) < (lam.end_lineno - lam.lineno):
                        lam = b
        if lam is not None and all(lam.lineno < l < lam.end_lineno for l in changed if abs(l - line) <= 1):
            # calls at run time, and calls made while some module was being imported
            return set(self.cls_lines.get(f, {}).get(line, ())) | self.imp_tests(f, line)
        # an edit whose every change lies inside one lambda body (checked on the ASTs)
        # is seen only by calls of that lambda; each call runs a line of the body
        body = None
        for node in local:
            if isinstance(node, ast.Lambda) and node.body.lineno <= line <= node.body.end_lineno:
                if body is None or (node.body.end_lineno - node.body.lineno) < (body.body.end_lineno - body.body.lineno):
                    body = node
        if body is not None and self._lambda_confined(f, ch, body):
            cl = self.cls_lines.get(f, {})
            sel = set()
            for l in range(body.body.lineno, body.body.end_lineno + 1):
                sel |= set(cl.get(l, ()))
                if l != body.lineno:  # the `lambda` line itself also runs when it is defined
                    sel |= self.imp_tests(f, l)
            log(f"  {f}:{line} confined to lambda body -> {len(sel)}")
            return sel
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

        owner = []

        def visit_owner(body, path):
            for n in body:
                if n.lineno <= line <= n.end_lineno and isinstance(n, ast.ClassDef):
                    owner[:] = path + [n.name]
                    visit_owner(n.body, path + [n.name])

        visit(tree.body)
        visit_owner(tree.body, [])
        expr_rule = self.expression_arg_types(f, stmt, owner)
        if expr_rule is not None:
            fl = self.file_level(f)
            # both are sound over-approximations, so their intersection is too
            return expr_rule & fl if fl is not None else expr_rule
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
            for g, glines in self._sources_with(name):
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

    # -------------------------------------------------------------- statement level
    def _line_map(self):
        if self._lines is None:
            import zlib

            import marshal

            p = MAPS.parent / "lines.bin.z"
            try:
                d = marshal.loads(zlib.decompress(p.read_bytes()))
                self._lines = {f: _Lazy(m, d["sets"]) for f, m in d["lines"].items()}
            except OSError:
                self._lines = {}
        return self._lines

    @staticmethod
    def _region(text_lines, s0, e):
        """Source of lines s0..e, dedented by the indentation of line s0."""
        import textwrap

        return textwrap.dedent("\n".join(text_lines[s0 - 1:e]))

    @staticmethod
    def _bindings(fn):
        import ast

        names, flags = set(), set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.id)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                flags.add((type(n).__name__, tuple(n.names)))
            elif isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)):
                flags.add(type(n).__name__)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
                names.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.arg):
                names.add(n.arg)
        return names, flags

    def statement_tests(self, f, unit, ch):
        """Tests that executed the statements a function-body edit touches, or None to
        fall back to the whole function. Sound fallbacks: a pure insertion, a statement
        no test executed, or any change to the names the function binds or to its
        yield/await/global/nonlocal (those change behaviour of other statements)."""
        import ast

        s0, e, b = unit
        blocks = [blk for blk in ch.blocks if s0 <= blk[0] <= e or (blk[1] and blk[0] + len(blk[1]) - 1 >= s0 and blk[0] <= e)]
        if not blocks or any(not blk[1] for blk in blocks):
            return None
        for blk in blocks:
            if not (b <= blk[0] and blk[0] + len(blk[1]) - 1 <= e):
                return None  # touches the header or spills outside the function
        src = pinned_source(f)
        if src is None:
            return None
        lines = src.split("\n")
        try:
            old_fn = ast.parse(self._region(lines, s0, e)).body[0]
            new_lines = lines[:]
            for first, removed, added in sorted(blocks, key=lambda x: x[0], reverse=True):
                new_lines[first - 1:first - 1 + len(removed)] = added
            new_e = e + sum(len(a) - len(r) for _, r, a in blocks)
            new_fn = ast.parse(self._region(new_lines, s0, new_e)).body[0]
        except (SyntaxError, IndexError, ValueError):
            return None
        if self._bindings(old_fn) != self._bindings(new_fn):
            return None
        # owning statement of each changed line (offsets: region line 1 == file line s0)
        owners = []
        changed = set()
        for first, removed, _ in blocks:
            changed.update(range(first, first + len(removed)))
        stmts = []
        for n in ast.walk(old_fn):
            if isinstance(n, ast.stmt) and n is not old_fn:
                kids = [c for fld in ("body", "orelse", "finalbody") for c in (getattr(n, fld, None) or [])]
                for h in getattr(n, "handlers", None) or []:
                    kids.append(h)
                hdr_end = (min(c.lineno for c in kids) - 1) if kids else n.end_lineno
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.decorator_list:
                    start = min(d.lineno for d in n.decorator_list)
                else:
                    start = n.lineno
                stmts.append((start + s0 - 1, max(hdr_end, n.lineno) + s0 - 1))
        for L in changed:
            best = None
            for a, z in stmts:
                if a <= L <= z and (best is None or z - a < best[1] - best[0]):
                    best = (a, z)
            if best is None:
                return None
            owners.append(best)
        lm = self._line_map().get(f, {})
        sel: set[int] = set()
        for a, z in owners:
            got = set()
            for L in range(a, z + 1):
                got.update(lm.get(L, ()))
            if not got:
                return None  # nobody executed it: fall back to the function
            sel |= got
        return sel

    # ------------------------------------------------------------ import-time exposure
    def imp_tests(self, f, line, only=None):
        """Importers of every module whose import ran f:line (optionally only those
        modules in `only`)."""
        out = set()
        for y in self.imp_line_mods.get(f, {}).get(line, ()):
            if only is None or y in only:
                out |= self.imp_importers[y]
        return out

    def _stmt_pairs(self, f, ch):
        """[(old_stmt, new_stmt)] for the module/class-level statements the edit touches,
        or None when they cannot be paired (then every importer is exposed)."""
        import ast

        src = pinned_source(f)
        old_t = self._tree(f)
        try:
            new_t = ast.parse(patched_source(src, ch))
        except (SyntaxError, TypeError):
            return None
        if old_t is None:
            return None
        lo = min(b[0] for b in ch.blocks) - 1
        hi = max(b[0] + max(len(b[1]), 1) for b in ch.blocks)

        def flat(tree):
            out = []

            def walk(body, path):
                for n in body:
                    out.append((path, n))
                    if isinstance(n, ast.ClassDef):
                        walk(n.body, path + (n.name,))
            walk(tree.body, ())
            return out

        a, b = flat(old_t), flat(new_t)
        if len(a) != len(b):
            return None
        pairs = []
        for (pa, x), (pb, y) in zip(a, b):
            if pa != pb or type(x) is not type(y):
                return None
            if isinstance(x, ast.ClassDef):
                continue  # its body statements are compared on their own
            if x.end_lineno < lo or x.lineno > hi:
                continue  # the patch leaves text outside its blocks unchanged
            if ast.dump(x) != ast.dump(y):
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue  # function edits are handled per statement elsewhere
                pairs.append((x, y, pa))
        return pairs

    @staticmethod
    def _may_raise(old, new) -> bool:
        """Can evaluating `new` at import fail where `old` did not? Only literals,
        containers, unary minus/plus/not, lambdas (not called here) and the names and
        attribute chains `old` already used are safe."""
        import ast

        def refs(n):
            out = set()
            for x in ast.walk(n):
                if isinstance(x, (ast.Name, ast.Attribute)):
                    out.add(ast.dump(x))
            return out

        def safe(n, known):
            if isinstance(n, ast.Constant):
                return True
            if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
                return all(safe(e, known) for e in n.elts)
            if isinstance(n, ast.Dict):
                return all(k is None or safe(k, known) for k in n.keys) and all(safe(v, known) for v in n.values)
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd, ast.Not)):
                return safe(n.operand, known)
            if isinstance(n, ast.Lambda):
                return all(safe(d, known) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None])
            if isinstance(n, (ast.Name, ast.Attribute)):
                return ast.dump(n) in known
            if isinstance(n, ast.Starred):
                return safe(n.value, known)
            return False

        if isinstance(new, ast.Assign) and isinstance(old, ast.Assign):
            if [ast.dump(t) for t in new.targets] != [ast.dump(t) for t in old.targets]:
                return True
            return not safe(new.value, refs(old.value))
        if isinstance(new, ast.AnnAssign) and isinstance(old, ast.AnnAssign):
            if ast.dump(new.target) != ast.dump(old.target) or ast.dump(new.annotation) != ast.dump(old.annotation):
                return True
            return new.value is not None and not safe(new.value, refs(old.value) if old.value is not None else set())
        return True

    def header_exposure(self, f, ch, inner):
        """A function header (decorators, defaults, annotations) is evaluated when the
        `def` runs, i.e. at import. If the edited header may now raise, every importer
        of those lines is exposed."""
        import ast

        everyone = set()
        for l in range(inner[0], inner[2]):
            everyone |= self.imp_tests(f, l)
        if not everyone:
            return set()
        src = pinned_source(f)
        try:
            old_t, new_t = ast.parse(src), ast.parse(patched_source(src, ch))
        except (SyntaxError, TypeError):
            return everyone
        lazy = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
                   for n in old_t.body)

        def find(t):
            for n in ast.walk(t):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    st = min([d.lineno for d in n.decorator_list] + [n.lineno])
                    if st == inner[0]:
                        return n
            return None

        a, b = find(old_t), find(new_t)
        if a is None or b is None or a.name != b.name:
            return everyone

        def exprs(n):
            out = list(n.decorator_list) + list(n.args.defaults) + [d for d in n.args.kw_defaults if d is not None]
            if not lazy:
                out += [x.annotation for x in n.args.args + n.args.kwonlyargs + n.args.posonlyargs if x.annotation is not None]
                out += [n.returns] if n.returns is not None else []
            return out

        ea, eb = exprs(a), exprs(b)
        if len(ea) != len(eb):
            return everyone
        for x, y in zip(ea, eb):
            if ast.dump(x) != ast.dump(y) and self._may_raise(
                    ast.Assign(targets=[ast.Name("_")], value=x), ast.Assign(targets=[ast.Name("_")], value=y)):
                return everyone
        return set()

    def import_exposure(self, f, ch):
        """Tests that a module/class-level edit can reach through an import:
          * if the new statement may raise, the importers of every import that ran it;
          * for each name it assigns, the importers of every import Y that ran a line
            mentioning that name, where the statement is in a class C, only if C (or a
            subclass) was used or defined during Y, or the line names C itself."""
        import ast

        sel: set[int] = set()
        pairs = self._stmt_pairs(f, ch)
        if pairs is None:
            for l in ch.lines:
                sel |= self.imp_tests(f, l)
            return sel, True
        def blank_lambdas(n):
            n = copy.deepcopy(n)
            for x in ast.walk(n):
                if isinstance(x, ast.Lambda):
                    x.body = ast.Constant(None)
            return ast.dump(n)

        import copy

        for old, new, path in pairs:
            if blank_lambdas(old) == blank_lambdas(new):
                # only lambda bodies changed: the value is otherwise identical, and a
                # changed body is seen only by its calls, which the lambda rule counts
                # (including calls made during imports)
                continue
            if self._may_raise(old, new):
                for l in range(old.lineno, old.end_lineno + 1):
                    sel |= self.imp_tests(f, l)
            names = set()
            for x in (old, new):
                tg = x.targets if isinstance(x, ast.Assign) else [x.target] if isinstance(x, ast.AnnAssign) else []
                for t in tg:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            touched = set(self.touched.get(f"{f}::{'.'.join(path)}", ())) if path else None
            cword = re.compile(r"\b" + re.escape(path[-1]) + r"\b") if path else None
            for name in names:
                word = re.compile(r"\b" + re.escape(name) + r"\b")
                for g, glines in self._sources_with(name):
                    gl = self.imp_line_mods.get(g)
                    if not gl:
                        continue
                    for i, text in enumerate(glines, 1):
                        if i in gl and name in text and word.search(text):
                            only = None if (touched is None or cword.search(text)) else touched
                            sel |= self.imp_tests(g, i, only)
        return sel, False

    def source(self, ch: FileChange):
        f = ch.old
        sel: set[int] = set()
        spans = self.spans.get(f)
        if spans is None:
            log(f"  {f}: unknown source file -> all")
            return None
        exposed_done = False
        for line in sorted(ch.lines):
            inner = None
            for s, e, b in spans:
                if s <= line <= e and (inner is None or e - s < inner[1] - inner[0]):
                    inner = (s, e, b)
            if inner is None:
                if not exposed_done:
                    exposed_done = True
                    exp_sel, _ = self.import_exposure(f, ch)
                    if exp_sel:
                        log(f"  {f}: import-time exposure -> {len(exp_sel)}")
                    sel |= exp_sel
                narrowed = self.class_level(f, line, ch)
                if narrowed is not None:
                    fl = self.file_level(f)
                    # both are sound over-approximations: keep only what both require
                    sel |= (narrowed & fl) if fl is not None else narrowed
                    continue
                fl = self.file_level(f)
                if fl is None:
                    return None
                log(f"  {f}:{line} module/class level -> {len(fl)}")
                sel |= fl
                continue
            ts, imp = self.units.get((f, inner[0], inner[1]), (set(), False))
            if line < inner[2]:
                hx = self.header_exposure(f, ch, inner)
                if hx:
                    log(f"  {f}:{line} header may raise at import -> {len(hx)}")
                sel |= hx
            if not imp and line >= inner[2]:
                st = self.statement_tests(f, inner, ch)
                if st is not None:
                    log(f"  {f}:{line} statement-level -> {len(st)} (function had {len(ts)})")
                    ts = st
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

    def maybe_doc_only(self, ch: FileChange) -> bool:
        """Cheap pre-check for same_code: every removed line is blank, a comment, or
        inside a docstring of the pinned file, and every added line is blank, a comment,
        or could be docstring text (the full syntax-tree check decides)."""
        touches_doc = False
        for first, removed, added in ch.blocks:
            for k, text in enumerate(removed):
                if self._in_docstring(ch.old, first + k):
                    touches_doc = True
                elif not _is_comment_or_blank(text):
                    return False
            if not removed and self._in_docstring(ch.old, first - 1) and self._in_docstring(ch.old, first):
                touches_doc = True
            if not removed:  # pure insertion: it must land inside a docstring or be a comment
                if not all(_is_comment_or_blank(x) for x in added) and not (
                        self._in_docstring(ch.old, first - 1) and self._in_docstring(ch.old, first)):
                    return False
        # comment/blank-only edits are proved harmless more cheaply by significant_lines
        return touches_doc

    def comparison_swap(self, ch: FileChange):
        """An edit whose only effect is to swap one comparison for its partner (is <-> ==,
        is not <-> !=, < <-> <=, > <-> >=) changes behaviour only where the two give
        different results. The map build recorded, for each comparison in sqlglot, the
        tests that ever evaluated it on such operands (maps/cmp.json). None = not this
        kind of edit, or the comparison also ran at import time."""
        import ast

        f = ch.old
        if len(ch.blocks) != 1:
            return None
        _first, removed, added = ch.blocks[0]
        if len(removed) != 1 or len(added) != 1:
            return None
        strip = lambda x: re.sub(r"\s*(?:\bis\s+not\b|\bis\b|==|!=|<=|>=|<|>)\s*", " ", x)
        if strip(removed[0]) != strip(added[0]):
            return None  # the line differs in more than a comparison operator
        if self._cmp is None:  # loaded only when an edit could be a comparison swap
            p = MAPS.parent / "cmp.json"
            self._cmp = json.loads(p.read_text()) if p.exists() else {"files": [], "diff": {}}
            self._cmp["files"] = set(self._cmp["files"])
        if f not in self._cmp["files"]:
            return None
        src = pinned_source(f)
        if src is None:
            return None
        # The two files differ only in operator characters on one line (checked above),
        # so their brackets, strings and statement boundaries are the same: comparing
        # the statement that holds that line is enough. Parse just that statement's old
        # and new text (falling back to whole files if it does not parse on its own).
        line = _first
        shift = 0
        old_t = self._tree(f)
        new_t = None
        if old_t is not None:
            holder = None
            node = old_t
            while True:
                nxt = None
                for c in ast.iter_child_nodes(node):
                    if getattr(c, "lineno", None) is not None and c.lineno <= line <= c.end_lineno:
                        nxt = c
                        break
                if nxt is None:
                    break
                if isinstance(nxt, ast.stmt):
                    holder = nxt
                node = nxt
            if holder is not None:
                lines = src.split("\n")
                l0, l1 = holder.lineno, holder.end_lineno
                old_txt = lines[l0 - 1:l1]
                new_txt = list(old_txt)
                new_txt[line - l0] = added[0]
                wrap = old_txt[0][:1] in (" ", "\t")
                pre = ["if 1:"] if wrap else []
                try:
                    o, n = ast.parse("\n".join(pre + old_txt)), ast.parse("\n".join(pre + new_txt))
                    o, n = (o.body[0].body if wrap else o.body), (n.body[0].body if wrap else n.body)
                    if len(o) == 1 and len(n) == 1:
                        old_t, new_t = o[0], n[0]
                        shift = l0 - 1 - len(pre)
                        line = line - shift
                except SyntaxError:
                    pass
        if new_t is None:
            try:
                old_t, new_t = ast.parse(src), ast.parse(patched_source(src, ch))
            except SyntaxError:
                return None
            line, shift = _first, 0
        snippet = not isinstance(old_t, ast.Module)

        def stmt_at(tree):
            node, best = tree, None
            while True:
                nxt = None
                for c in ast.iter_child_nodes(node):
                    if getattr(c, "lineno", None) is not None and c.lineno <= line <= c.end_lineno:
                        nxt = c
                        break
                if nxt is None:
                    return best
                if isinstance(nxt, ast.stmt):
                    best = nxt
                node = nxt

        if not snippet:
            old_t, new_t = stmt_at(old_t), stmt_at(new_t)
        if old_t is None or new_t is None or (old_t.lineno, old_t.end_lineno) != (new_t.lineno, new_t.end_lineno):
            return None
        a_nodes, b_nodes = list(ast.walk(old_t)), list(ast.walk(new_t))
        if len(a_nodes) != len(b_nodes):
            return None
        swaps = []
        for a, b in zip(a_nodes, b_nodes):
            if type(a) is not type(b):
                if not isinstance(a, ast.cmpop) or not isinstance(b, ast.cmpop):
                    return None
            elif isinstance(a, ast.Compare) and [type(o) for o in a.ops] != [type(o) for o in b.ops]:
                swaps.append((a, b))
        if len(swaps) != 1:
            return None
        a, b = swaps[0]
        partner = _cmp_partner()
        if len(a.ops) != 1 or len(b.ops) != 1 or partner.get(type(a.ops[0]), (None,))[0] is not type(b.ops[0]):
            return None
        b.ops = a.ops
        if ast.dump(old_t) != ast.dump(new_t):
            return None  # something else changed too
        key = f"{f}:{a.lineno + shift}:{a.col_offset}:{partner[type(a.ops[0])][1]}"
        if key not in self._cmp["diff"]:
            log(f"  {f}:{a.lineno + shift} comparison swap never differs -> nothing")
            return set()
        tests = self._cmp["diff"][key]
        if tests is None:
            return None
        log(f"  {f}:{a.lineno + shift} comparison swap differs in {len(tests)} tests")
        return set(tests)

    def select(self, changes: list[FileChange]) -> set[int]:
        sel: set[int] = set()
        for ch in changes:
            path = ch.new if ch.added else ch.old
            log(f" {path} lines={sorted(ch.lines)[:12]} added={ch.added} deleted={ch.deleted}")
            if path.startswith("sqlglot/typing/"):
                # tests/test_typing.py compares annotator code objects, whose constants
                # carry docstrings and nested line numbers: it sees even layout edits
                sel |= {n for i, n in self.ids.items() if i.startswith("tests/test_typing.py::")}
            if path.endswith(DOC_EXT) or path.startswith(("docs/", "posts/", "pdoc/", ".github/")):
                continue
            if path.startswith("tests/fixtures/"):
                r = self.fixture(ch) if not ch.deleted else None
            elif ch.added or ch.deleted:
                r = None
            elif path.startswith("sqlglot/") and path.endswith(".py") and (sw := self.comparison_swap(ch)) is not None:
                r = sw
            elif path.startswith("sqlglot/") and path.endswith(".py") and self.maybe_doc_only(ch) and same_code(ch):
                log(f"  {path}: same AST apart from docstrings -> nothing")
                continue
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
