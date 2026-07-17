FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent gateway

COPY pyproject.toml requirements.lock README.md ./
COPY app ./app
RUN pip install --no-cache-dir --constraint requirements.lock .

USER 10001:10001
EXPOSE 8788
CMD ["python", "-m", "app.run"]
