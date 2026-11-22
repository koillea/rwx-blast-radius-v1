# RWX Blast Radius submission.
# The selector needs only Python and the maps committed in maps/ (built once from the
# pinned sqlglot commit by build/; see README.md). No network is used at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY selector.py fixcheck.py /app/
COPY maps/maps.bin.z maps/lines.bin.z maps/cmp.json maps/dicts.bin.z maps/attrs.bin.z maps/pinned_src.zip maps/fixtures.bin.z /app/maps/
# v25: the pinned sqlglot and tests sources (already in pinned_src.zip), unpacked and
# byte-compiled at build time, for the fixture check. Fixture files are not unpacked.
RUN python -c "import zipfile; zipfile.ZipFile('/app/maps/pinned_src.zip').extractall('/app/pinned')" \
    && python -m compileall -q /app/pinned /app/selector.py /app/fixcheck.py

CMD ["python3", "-S", "/app/selector.py"]
