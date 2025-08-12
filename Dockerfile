FROM selenium/standalone-chrome:latest

# allow Selenium Manager to resolve a matching chromedriver
ENV SE_OFFLINE=false

USER root
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 10000

# run your app (override base ENTRYPOINT so our script starts)
ENTRYPOINT ["python3","/app/app.py"]
