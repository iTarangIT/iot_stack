FROM python:3.12.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY poll.py .
RUN useradd --create-home --shell /usr/sbin/nologin app
USER app
CMD ["python", "-u", "poll.py"]
