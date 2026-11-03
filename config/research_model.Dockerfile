# Build explicitly on the execution host. The research agent never pulls images.
# No repository sources, credentials or network tools are copied into this image.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /output
ENTRYPOINT ["python", "-I", "-B", "/input/model.py"]
