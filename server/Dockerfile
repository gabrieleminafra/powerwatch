FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static ./static

ENV PW_DB=/data/powerwatch.db
VOLUME /data
EXPOSE 8080 9999/udp

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PW_HTTP_PORT','8080')+'/healthz', timeout=8).status==200 else 1)"

CMD ["python3", "-u", "powerwatch.py"]
