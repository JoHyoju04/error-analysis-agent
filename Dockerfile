# Python 3.12: the Codex SDK needs 3.10+, 3.11+ is recommended.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: the bot prepares source snapshots. openssh-client is only needed when the
# bot itself fetches over SSH; prefer preparing repos on the host instead
# (scripts/prepare-repo.sh) and leaving no key inside this container.
# tini: reaps the `codex app-server` child process on shutdown.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates openssh-client tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/bot --shell /usr/sbin/nologin bot

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY prompts ./prompts
# Example mapping only; compose mounts the real config.local.yaml over it.
COPY config.yaml ./config.yaml

# CODEX_HOME and the workspace are mount points; they must belong to the
# container user or the OAuth session cannot be written.
RUN mkdir -p /home/bot/.codex /workspace/repos /workspace/snapshots /workspace/state \
    && chown -R bot:bot /home/bot /workspace

USER bot

ENV CODEX_HOME=/home/bot/.codex \
    CONFIG_PATH=/app/config.yaml

# Cheap liveness probe: validates configuration without any network call.
HEALTHCHECK --interval=60s --timeout=20s --start-period=20s --retries=3 \
    CMD ["python", "-m", "error_analysis_agent.selfcheck", "--only", "config"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "error_analysis_agent.main"]
