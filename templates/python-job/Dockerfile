FROM python:3.13.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

# Annex runs every app as uid 10001 on a read-only filesystem
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

# On Annex the container idles (`sleep infinity`) and each scheduled run is
# `docker exec`'d with the command from annex.yaml. Locally this runs the job once.
CMD ["python", "job.py"]
