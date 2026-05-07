FROM python:3.12-slim

WORKDIR /app

# git is required by pip to install anker-solix-api from GitHub
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Data directory (mount a volume here for persistence)
RUN mkdir -p /data

EXPOSE 8080

CMD ["gunicorn", "passenger_wsgi:application", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
