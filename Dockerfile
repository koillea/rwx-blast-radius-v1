# RWX Blast Radius submission.
# The selector needs only Python and the maps committed in maps/ (built once from the
# pinned sqlglot commit by build/; see README.md). No network is used at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY selector.py tpcds_check.py /app/
COPY maps/maps.bin.z maps/lines.bin.z maps/cmp.json maps/dicts.bin.z maps/attrs.bin.z maps/pinned_src.zip /app/maps/
COPY maps/tpcds /app/maps/tpcds
RUN python -m py_compile /app/selector.py /app/tpcds_check.py

# Pinned sqlglot sources for the TPC-DS check (tpcds_check.py), precompiled, and the
# pinned fingerprints recomputed with this image's Python. If any of this fails, the
# check simply never matches and test_execute_tpcds stays selected as usual.
RUN python -c "import zipfile; z = zipfile.ZipFile('/app/maps/pinned_src.zip'); [z.extract(n, '/app/pinned') for n in z.namelist() if n.startswith('sqlglot/')]" \
 && python -m compileall -q /app/pinned/sqlglot \
 && cd /app && PYTHONHASHSEED=0 python -S tpcds_check.py pin /app/maps/tpcds/pinned.json \
 || true

CMD ["python3", "-S", "/app/selector.py"]
