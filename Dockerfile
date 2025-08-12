# Use Chrome + Chromedriver preinstalled
FROM selenium/standalone-chrome:latest

# Install Python
USER root
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

# Copy code and deps
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app.py .

# Helpful (not required) to document the port Render expects
EXPOSE 10000

# CRUCIAL: override the image's default ENTRYPOINT so our app runs
ENTRYPOINT ["python3","/app/app.py"]
