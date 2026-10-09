FROM python:3.13-slim
WORKDIR /app
COPY pipeline/requirements.txt pipeline/requirements.txt
RUN pip install --no-cache-dir -r pipeline/requirements.txt
COPY pipeline/ pipeline/
COPY db/ db/
WORKDIR /app/pipeline
ENV PYTHONUNBUFFERED=1 SCHEMA_PATH=/app/db/schema.sql DATA_DIR=/data
# runs once: load + reconcile + analyse, then exit (idempotent; safe to re-run)
CMD ["python", "-m", "dcc.run"]
