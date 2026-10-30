# Build only this directory as the context. Repository configs and .env files
# must never be sent to the Docker build daemon.
ARG BASE_IMAGE=python:3.10-slim-bookworm
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
COPY requirements.txt /tmp/research-requirements.txt
RUN pip install --no-cache-dir -r /tmp/research-requirements.txt \
    && rm /tmp/research-requirements.txt \
    && mkdir -p /input /output \
    && chmod 755 /input /output
USER 65532:65532
WORKDIR /input/code
CMD ["python", "-B", "-m", "src.interactive.assistant.research_worker", "execute"]
