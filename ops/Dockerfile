FROM python:3.13.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY annex_ops annex_ops

# read-only filesystem, uid 10001, like the apps
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin ops
USER 10001

CMD ["uvicorn", "annex_ops.app:app", "--host", "0.0.0.0", "--port", "8000"]
