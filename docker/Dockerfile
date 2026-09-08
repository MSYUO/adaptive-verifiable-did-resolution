# Single image serving both roles. The compose file selects the role via the
# service command, which keeps router and resolver dependency sets identical
# and avoids a second near-duplicate Dockerfile.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY service_assets ./service_assets
COPY web ./web

# Overridden per service in docker-compose.yml.
CMD ["uvicorn", "avdr.router.app:app", "--host", "0.0.0.0", "--port", "8000"]
