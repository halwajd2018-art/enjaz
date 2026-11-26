# Build-time-only LiveKit SDK acquisition; no npm or external CDN at browser runtime.
# Keep this version aligned with VIDEO_SDK_CDN in main.py until CDN is disabled.
FROM node:22-alpine AS livekit-sdk
COPY app.js /tmp/enjaz-app.js
RUN node --check /tmp/enjaz-app.js
RUN set -eux; \
    npm pack livekit-client@2.25.0 --ignore-scripts --pack-destination /tmp --silent; \
    mkdir -p /opt/livekit; \
    tar -xzf /tmp/livekit-client-2.25.0.tgz -C /opt/livekit \
      package/dist/livekit-client.umd.js; \
    test -s /opt/livekit/package/dist/livekit-client.umd.js

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
COPY --from=livekit-sdk /opt/livekit/package/dist/livekit-client.umd.js /app/app/static/livekit-client.umd.min.js
RUN python -m py_compile /app/app/main.py
# All tests use a temporary SQLite DB and stub LiveKit; no production DB or keys.
COPY tests /app/tests
RUN python -m unittest discover -s tests -p 'test_direct_calls.py' -v \
    && rm -rf /app/tests

USER enjaz

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
