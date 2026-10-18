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

# Optional extras for this build. The eval worker passes `evals-audio` (Kokoro
# speaks the caller, Moonshine transcribes the bot for the judge, ~66MB); the
# API image takes none, since nothing on the call path ever loads them.
ARG INSTALL_EXTRAS=""

# The dependencies first, read from pyproject.toml alone, then the package with
# --no-deps: a change to src/ reuses the dependency layer instead of reinstalling
# the whole pipecat/ML stack. The BuildKit pip cache mount doesn't survive
# between CI runners; the layer does, in the GitHub Actions cache. Prod deps
# only — no [dev].
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import sys, tomllib; \
p = tomllib.load(open("pyproject.toml", "rb"))["project"]; \
extras = [e for e in sys.argv[1].split(",") if e]; \
print("\n".join(p["dependencies"] + [d for e in extras for d in p["optional-dependencies"][e]]))' \
        "$INSTALL_EXTRAS" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt

# hatchling builds the wheel from src/, so both must be present.
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps .

# No NLTK step. Pipecat's TTS sentence splitting used NLTK and needed its
# `punkt_tab` data baked in, or the runtime hit "Resource punkt_tab not found"
# mid-call. 1.12 replaced it with sentencex, which carries its own rules and
# downloads nothing — so the step it replaced now fails the build outright
# with `No module named 'nltk'`.

# ---- runtime: slim image, just the venv + migrations, no compilers ----
FROM python:3.12-slim AS runtime

# TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS empty: redaction's email recognizer
# (Presidio, via tldextract) would otherwise fetch the Public Suffix List from
# GitHub on its first email, and hang behind a firewall that drops packets.
# Empty means the snapshot tldextract ships, offline. Redaction's other
# runtime download, spaCy installing a missing model, is refused in code
# (services/redaction.py): the model is a dependency, installed above.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS=""

WORKDIR /app

# PyAV/OpenCV (pulled by aiortc for WebRTC) dlopen these at runtime — not bundled
# in their wheels. Without them WebRTC connect fails. libxcb1 + libGL + glib are
# the standard headless video cluster.
# Deliberately NOT libportaudio2: the evals-audio extra pulls moonshine, which
# depends on sounddevice, whose import raises "PortAudio library not found"
# without it — but only its mic-capture module imports sounddevice, and nothing
# a server runs does. Verified in this image: moonshine_voice, MoonshineSTTService
# and KokoroTTSService all import clean. Don't add it on the strength of the
# dependency alone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Non-root. storage/ is the default local object-storage backend → must be writable.
# `/home/app/.cache` is created here, empty and owned by app, on purpose: it is
# where the eval worker's audio models land (Kokoro, Moonshine), and where a
# deployment mounts a volume to keep them across recreates. Docker copies an
# image directory's ownership into a fresh named volume — but only if the
# directory exists. Without this the mount point is created root-owned and the
# non-root process gets `PermissionError: /home/app/.cache/pipecat` on the
# first audio run, which no import check can catch.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/storage /home/app/.cache \
    && chown -R app:app /app /home/app/.cache

COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app alembic/ alembic/
COPY --chown=app:app alembic.ini ./

USER app

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8090/live').raise_for_status()"

CMD ["python", "-m", "turncall"]
