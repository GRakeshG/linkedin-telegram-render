FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN mkdir -p /profile
ENV PROFILE_ROOT=/profile
ENV PORT=8080

CMD ["python", "app.py"]