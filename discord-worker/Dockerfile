FROM python:3.12-slim

# Trim the image so vulnerability scanners have less to flag and so the
# container starts faster on the e2-micro VM.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY worker.py /app/worker.py

# Run as a non-root user; matches Cloud Run / Container-Optimized OS
# expectations and limits damage if the worker were ever compromised.
RUN useradd --create-home --shell /usr/sbin/nologin worker
USER worker

CMD ["python", "/app/worker.py"]
