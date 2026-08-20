FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --upgrade pip && pip install .

RUN mkdir -p /data

EXPOSE 8787

ENV DATABASE_URL=sqlite:////data/landplots.db \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8787

CMD ["python", "-m", "app.web"]
