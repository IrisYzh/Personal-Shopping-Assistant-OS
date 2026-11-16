# Pilot backend: API for the Chrome extension, plus price checks and emails on a schedule.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PILOT_TIMEZONE=America/Los_Angeles

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shopping_agent ./shopping_agent
COPY send_email.py ./

# User data, analytics and job state live here. Mount a persistent disk at /app/data.
RUN mkdir -p /app/data/users
VOLUME ["/app/data"]

EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8787\")}/health')"
CMD ["python", "-m", "shopping_agent", "serve", "--host", "0.0.0.0", "--port", "8787", "--data-dir", "/app/data/users", "--jobs"]
