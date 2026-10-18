# syntax=docker/dockerfile:1
# Official multi-architecture Python image. Update tag and digest together.
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/El_Salvador
WORKDIR /app
RUN groupadd --gid 10001 exporter \
    && useradd --uid 10001 --gid exporter --no-create-home exporter \
    && mkdir /data \
    && chown exporter:exporter /data
COPY asana_time_export.py /app/asana_time_export.py

FROM base AS test
COPY asana_postgres.py /app/asana_postgres.py
COPY asana_sheets.py /app/asana_sheets.py
COPY tests/ /app/tests/
USER 10001:10001
RUN python -m unittest discover -s tests -q

FROM base AS runtime
USER 10001:10001
ENTRYPOINT ["python", "/app/asana_time_export.py"]
CMD ["--help"]
