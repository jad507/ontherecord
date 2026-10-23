# On the Record in a container, for Linux machines and servers with an NVIDIA GPU (or none). See docker/README.md.
# The packages come from uv.lock (torch 2.11 with CUDA 12.8; `--build-arg TORCH=cu126` for cards older than Turing,
# `cpu` for none), ffmpeg from Debian, and llama-server is the pinned llama.cpp build b11249 for CUDA 12.8, checked
# against its published SHA-256 like the desktop installers do. CUDA 12 builds run on hosts with CUDA 13 drivers.
FROM python:3.12-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# uv by digest as well as tag: a tag can be moved to other bytes, a digest cannot
COPY --from=ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
ARG TORCH=cu128
RUN uv sync --extra all --frozen --no-install-project --no-default-groups --group ${TORCH}
COPY src ./src
COPY recipes ./recipes
COPY install ./install
RUN uv sync --extra all --frozen --no-default-groups --group ${TORCH}

# llama-server, CUDA build plus its CUDA runtime libraries (the NVIDIA driver itself comes from the host)
RUN /app/.venv/bin/python -c "\
from pathlib import Path; from ontherecord import bootstrap as b; \
p = b.ensure_llama(Path('/opt/llama'), {'os': 'linux', 'arch': 'x86_64'}, builds=['cuda-12'], verify=False); \
Path('/opt/llama/server').symlink_to(p)" && rm -rf /opt/llama/downloads

COPY docker/machine.yaml /etc/ontherecord/machine.yaml
ENV ONTHERECORD_CONFIG=/etc/ontherecord/machine.yaml HF_HOME=/data/huggingface PATH=/app/.venv/bin:$PATH \
    ONTHERECORD_HOME=/data HOME=/data USER=ontherecord
VOLUME /data
EXPOSE 8765
# 0.0.0.0 inside the container only; docker-compose.yml publishes it on the host's 127.0.0.1
CMD ["ontherecord", "serve", "--host", "0.0.0.0", "--port", "8765"]
