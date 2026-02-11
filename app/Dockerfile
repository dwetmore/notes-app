FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY static ./static

ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000

RUN useradd -m -u 10001 appuser
USER 10001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
