FROM python:3.13.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py .

# Annex runs every app as uid 10001 on a read-only filesystem
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --start-interval=2s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/healthz', timeout=2)"

CMD ["python", "app.py"]
