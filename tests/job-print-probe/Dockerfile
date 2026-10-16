FROM python:3.13.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY job.py .

# Annex runs every app as uid 10001 on a read-only filesystem
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

CMD ["python", "job.py"]
