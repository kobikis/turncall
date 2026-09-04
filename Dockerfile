# syntax=docker/dockerfile:1
# ---- builder: compile deps into a venv, kept out of the final image ----
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# gcc only: build any deps without a manylinux wheel. asyncpg speaks the PG wire
# protocol directly, so no libpq needed; audio/webrtc wheels bundle their libs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install CPU-only torch FIRST so the project resolve doesn't drag in the default
# Linux CUDA build (torch + nvidia-* cu* wheels ≈ 2.5GB of GPU libs this CPU
# voice/ONNX workload never runs). pipecat's silero/smart-turn extras need torch;
# the >= ranges are satisfied by the pre-installed CPU wheels, so pip won't
# replace them. If a version conflict ever appears, pin to match pipecat here.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio

# hatchling builds the wheel from src/, so both must be present. The BuildKit pip
# cache mount keeps wheels across builds (outside image layers) so a code change
# doesn't re-download the heavy pipecat/ML stack. Prod deps only — no [dev].
COPY pyproject.toml ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/pip pip install .

# Pipecat's TTS services sentence-split with NLTK; bake the tokenizer data into
# the venv so the runtime never hits "Resource punkt_tab not found" mid-call.
RUN python -m nltk.downloader -d /opt/venv/nltk_data punkt_tab

# ---- runtime: slim image, just the venv + migrations, no compilers ----
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# PyAV/OpenCV (pulled by aiortc for WebRTC) dlopen these at runtime — not bundled
# in their wheels. Without them WebRTC connect fails. libxcb1 + libGL + glib are
# the standard headless video cluster.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Non-root. storage/ is the default local object-storage backend → must be writable.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/storage \
    && chown -R app:app /app

COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app alembic/ alembic/
COPY --chown=app:app alembic.ini ./

USER app

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8090/live').raise_for_status()"

CMD ["python", "-m", "turncall"]
