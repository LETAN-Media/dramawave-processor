FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g jianying-subtitle \
    && jianying-subtitle --help >/dev/null 2>&1 || true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /tmp/dramawave-processor /var/lib/dramawave-processor/storage

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
