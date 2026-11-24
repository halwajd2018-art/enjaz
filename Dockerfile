
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --shell /usr/sbin/nologin enjaz \
    && mkdir -p /app/app/static

COPY main.py /app/app/main.py
COPY __init__.py /app/app/__init__.py
COPY index.html app.js style.css /app/app/static/

USER enjaz

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
