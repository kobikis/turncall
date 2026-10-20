# syntax=docker/dockerfile:1
# Three build stages, so a change reaches only the layers it has to:
#   requirements  uv.lock's production dependencies, exported as a lock file;
#                 dev tools and tool settings leave it unchanged
#   deps          a venv holding exactly those, rebuilt only when the export
#                 changes
#   wheel         TurnCall itself, built from src/, installed last in runtime
# CI keeps every layer in the GitHub Actions cache (cache-to mode=max), so a
# code change rebuilds the wheel and one small layer, not the 2GB venv.

# uv, pinned: it reads uv.lock and installs the export. A bump is deliberate.
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

# ---- requirements ----
FROM python:3.12-slim AS requirements

# Optional extras for this build. The eval worker passes `evals-audio` (Kokoro
# speaks the caller, Moonshine transcribes the bot for the judge, ~66MB); the
# API image takes none, since nothing on the call path ever loads them.
ARG INSTALL_EXTRAS=""

# The versions are uv.lock's, never a fresh resolve (#377): a release published
# upstream reaches the image only through a commit that changes the lock.
# --frozen reads the lock as it is; CI's `uv sync --locked` is what fails a
# lock that no longer matches pyproject.toml. The export is a pylock.toml
# (PEP 751), which names each wheel by URL and hash, so torch comes from the
# PyTorch CPU index uv.lock chose and the deps stage resolves nothing.
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-header \
        --format pylock.toml \
        $(echo "$INSTALL_EXTRAS" | tr ',' '\n' | sed '/^$/d; s/^/--extra /') \
        > /pylock.toml

# ---- deps: install the dependencies into a venv, kept out of the final image ----
FROM python:3.12-slim AS deps

# UV_COMPILE_BYTECODE: write .pyc at install, as pip did, so a container does
# not compile the tree on every start. UV_LINK_MODE=copy: the cache is a mount,
# which a hardlink cannot cross.
ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# gcc only: build any deps without a manylinux wheel. asyncpg speaks the PG wire
# protocol directly, so no libpq needed; audio/webrtc wheels bundle their libs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# A stdlib venv, so it carries the pip the runtime stage installs the wheel with.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Keyed on the export's content, not pyproject.toml's: a new dev dependency or a
# ruff setting no longer reinstalls the stack. Torch is the CPU-only build
# ([tool.uv.sources] in pyproject.toml): PyPI's Linux torch would add ~2.5GB of
# CUDA libraries this CPU voice/ONNX workload never runs. The BuildKit cache
# mount doesn't survive between CI runners; this layer does. Prod deps only.
# pylock.toml installs are a uv preview feature, hence the flag; uv is pinned.
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=requirements /pylock.toml /tmp/pylock.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --preview-features pylock --python /opt/venv/bin/python \
        -r /tmp/pylock.toml

# ---- wheel: TurnCall itself ----
FROM python:3.12-slim AS wheel

# hatchling builds the wheel from src/, so both must be present.
COPY pyproject.toml ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --disable-pip-version-check --no-deps --wheel-dir /wheels .

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

COPY --from=deps --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app alembic/ alembic/
COPY --chown=app:app alembic.ini ./

USER app

# TurnCall last, on top of the dependencies: a code change adds this small
# layer and leaves the venv layer above as it was. The wheel is bind-mounted,
# so it never lands in the image.
RUN --mount=type=bind,from=wheel,source=/wheels,target=/tmp/wheels \
    pip install --disable-pip-version-check --no-cache-dir --no-deps /tmp/wheels/*.whl

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8090/live').raise_for_status()"

CMD ["python", "-m", "turncall"]
