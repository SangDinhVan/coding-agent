# Python 3.14.7 slim-bookworm, linux/amd64 manifest digest resolved 2026-09-12.
FROM python@sha256:d893452fcd120ea9a7233972c85ea868255bde289a636fe76ff090427fe8fac9

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/nonexistent

COPY requirements.lock /opt/coding-agent/requirements.lock
RUN python -m pip install --require-hashes -r /opt/coding-agent/requirements.lock \
    && groupadd --gid 65532 sandbox \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sandbox \
    && mkdir -p /opt/coding-agent/bin \
    && chmod 0755 /opt/coding-agent /opt/coding-agent/bin

COPY sandbox_fs.py sandbox_exec.py /opt/coding-agent/bin/
RUN chmod 0555 /opt/coding-agent/bin/sandbox_fs.py /opt/coding-agent/bin/sandbox_exec.py

USER 65532:65532
WORKDIR /workspace
CMD ["python", "-c", "import signal; signal.signal(signal.SIGTERM, lambda *_: exit(0)); signal.pause()"]
