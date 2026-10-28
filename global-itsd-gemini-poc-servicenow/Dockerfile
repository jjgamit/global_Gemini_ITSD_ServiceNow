FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r bridge/requirements.txt

WORKDIR /app/bridge

CMD ["sh", "-c", "exec python main.py --host 0.0.0.0 --port ${PORT:-8444} --verbose"]
