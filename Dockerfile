# TradingAgents image — dual-purpose container that the docker-compose
# services reuse:
#
#   `web`     → command = ["serve", "--host", "0.0.0.0", "--port", "8000"]
#   `cli`     → command = whatever the user passes (analyze / batch)
#   `migrate` → entrypoint override = ["python", "-m",
#                                     "tradingagents.persistence", "upgrade"]
#
# All three share this single image; compose just varies command +
# entrypoint per service. The `tradingagents` console script is the
# default ENTRYPOINT — `tradingagents serve` / `tradingagents analyze`
# etc. work without extra wrapping.

# ----------------------------- build stage -----------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Copy only the package metadata first so the dependency-install layer
# caches across code-only changes. Source still has to be present for
# `pip install .` (since pyproject uses setuptools.packages.find), so
# we follow the metadata copy with the full source copy. Re-running
# `pip install .` is fast once the deps are in /opt/venv.
COPY pyproject.toml ./
COPY tradingagents ./tradingagents
COPY cli ./cli
COPY webui ./webui
COPY scripts ./scripts
COPY main.py ./

RUN pip install .

# ------------------------------ run stage ------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime native deps. WeasyPrint (PDF export) needs Pango + Cairo at
# runtime; fonts-dejavu-core gives it a usable fallback when the report
# requests Helvetica / Arial. Everything else is pure Python.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Carry the prebuilt venv over from the builder.
COPY --from=builder /opt/venv /opt/venv

# Non-root runtime user. `~/.tradingagents` is the legacy state root
# (markdown log, JSON cache, checkpoints) — mounted as a named volume
# in compose so state survives container restarts even in DB-off mode.
RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser
WORKDIR /home/appuser/app

# Carry the source over from the builder so editable-install-style
# refs (e.g. `python -m tradingagents.persistence`) resolve at runtime
# without dragging dev tools into the runtime image.
COPY --from=builder --chown=appuser:appuser /build .

ENTRYPOINT ["tradingagents"]
