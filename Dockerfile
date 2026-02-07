FROM python:3.12-slim

# Basic hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY app.py /app/app.py

# Create non-root user (recommended)
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Defaults: run once and exit
ENTRYPOINT ["python", "/app/app.py"]
CMD ["run"]
