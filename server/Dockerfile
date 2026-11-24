# Образ сервера «Беседа»
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Сервер работает не от root
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin beseda \
 && mkdir -p /data && chown beseda:beseda /data
COPY --chown=root:root beseda_server ./beseda_server

ENV BESEDA_DATA=/data \
    BESEDA_HOST=0.0.0.0 \
    BESEDA_PORT=8765 \
    BESEDA_UDP_HOST=0.0.0.0 \
    BESEDA_UDP_PORT=50000

USER beseda
VOLUME ["/data"]
EXPOSE 8765/tcp 50000/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/info' % os.environ.get('BESEDA_PORT','8765'), timeout=4)"

CMD ["python", "-m", "beseda_server"]
