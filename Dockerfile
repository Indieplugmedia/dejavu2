FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libpq-dev \
    libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt setup.py setup.cfg MANIFEST.in ./
COPY dejavu ./dejavu
COPY app.py ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir .

ENV PORT=8000
ENV MPLBACKEND=Agg
ENV PYTHONUNBUFFERED=1
ENV APP_BUILD=2026-08-19-delete-reindex
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
