# syntax=docker/dockerfile:1
FROM docker:29.0.1-cli@sha256:f7d048590d889e00868920f658e807ecbc4ef662d51c9af67eb5534a447d58d6 AS docker-cli
FROM python@sha256:d893452fcd120ea9a7233972c85ea868255bde289a636fe76ff090427fe8fac9

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /opt/coding-agent
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
COPY docker-entrypoint.sh /usr/local/bin/coding-agent-entrypoint
RUN chmod 0555 /usr/local/bin/coding-agent-entrypoint

ENTRYPOINT ["/usr/local/bin/coding-agent-entrypoint"]
