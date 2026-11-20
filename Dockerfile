# RWX Blast Radius submission.
# The selector needs only Python and the maps committed in maps/ (built once from the
# pinned sqlglot commit by build/; see README.md). No network is used at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY selector.py /app/selector.py
COPY maps/maps.json maps/pinned_src.zip /app/maps/
RUN python -m py_compile /app/selector.py

CMD ["python3", "-S", "/app/selector.py"]
