FROM python:3.12-slim

# Bare-minimum analysis stack. Pinned to avoid surprises.
RUN pip install --no-cache-dir --root-user-action=ignore \
    "pandas==2.2.3" \
    "numpy==2.1.3" \
    "scipy==1.14.1" \
    "fastapi==0.115.6" \
    "uvicorn==0.32.1" \
    "pydantic==2.10.4"

# Run as non-root for defense in depth (the docker-compose layer also turns
# off network and adds read-only fs).
RUN useradd --create-home --shell /usr/sbin/nologin sandbox
USER sandbox
WORKDIR /home/sandbox

COPY --chown=sandbox:sandbox executor.py /home/sandbox/executor.py

# Bind to 0.0.0.0 inside container; docker-compose maps to 127.0.0.1:8091.
EXPOSE 8000
CMD ["python", "-u", "executor.py"]
