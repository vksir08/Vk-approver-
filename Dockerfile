FROM python:3.11.9-slim

WORKDIR /app

# System build tools (needed for compiling C extensions in aiohttp/pymongo)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ make && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy bot source
COPY main.py .

# Render injects PORT env var automatically
EXPOSE 10000

CMD ["python", "main.py"]