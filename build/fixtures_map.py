"""Build maps/fixtures.bin.z: the pinned text of every SQL fixture under tests/fixtures/,
as Python's open(..., encoding="utf-8") returns it (universal newlines).
usage: python3 build/fixtures_map.py <pinned sqlglot checkout> maps/fixtures.bin.z"""
import marshal, os, sys, zlib
root, out = sys.argv[1], sys.argv[2]
fx = os.path.join(root, "tests", "fixtures")
d = {}
for dp, _, fs in os.walk(fx):
    for f in fs:
        if f.endswith(".sql"):
            p = os.path.join(dp, f)
            with open(p, encoding="utf-8") as fh:
                d[os.path.relpath(p, fx).replace(os.sep, "/")] = fh.read()
open(out, "wb").write(zlib.compress(marshal.dumps(d), 9))
print(len(d), "fixtures", sum(map(len, d.values())), "chars")
