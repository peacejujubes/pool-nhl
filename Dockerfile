FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY data_seed ./data_seed

# SQLite DB lives here; mount a host folder on top of it (see docker-compose.yml)
# so the data survives rebuilds/updates.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000
ENV POOL_DB_PATH=/app/data/pool.db

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
