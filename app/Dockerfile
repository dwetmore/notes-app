FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY static ./static

ENV DB_PATH=/data/notes.db
EXPOSE 8000

# Run as non-root
RUN useradd -m -u 10001 appuser \
    && mkdir -p /data \
    && chown -R 10001:10001 /data
USER 10001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
