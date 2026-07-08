FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY media_cleanup_audit.py .
COPY config.example.yml .

EXPOSE 6996

ENTRYPOINT ["python", "/app/media_cleanup_audit.py"]
CMD ["--serve", "--config", "/app/config.yml", "--output-dir", "/reports", "--port", "6996"]
