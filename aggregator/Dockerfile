FROM python:3.12-slim

WORKDIR /app

# System deps for psycopg c-extension build (libpq) — keep image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps in a layer separate from code so rebuilds are fast
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the package
COPY scheduler.py .
COPY jobs ./jobs
COPY lib  ./lib

# Run as non-root for safety
RUN useradd --create-home --shell /usr/sbin/nologin app
USER app

# unbuffered so docker logs streams in real time
CMD ["python", "-u", "scheduler.py"]
