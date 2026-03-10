FROM python:3.11-slim

# System deps needed by osmnx / shapely / pyproj
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    libspatialindex-dev \
    libproj-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# OSMnx disk cache dir — writable at runtime
RUN mkdir -p /app/.osmnx_cache && chmod 777 /app/.osmnx_cache

# Render / Railway inject PORT at runtime; default to 8000 locally
ENV PORT=8000

EXPOSE 8000

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
