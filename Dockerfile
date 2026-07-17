FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3

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
