FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
ENV DATA_DIR=/data
ENV SECRET_KEY=""
ENV ADMIN_PASSWORD="admin123"
ENV BINANCE_API_KEY=""
ENV BINANCE_API_SECRET=""

RUN mkdir -p /data

EXPOSE 8000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120"]
