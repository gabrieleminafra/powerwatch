FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static ./static

ENV PW_DB=/data/powerwatch.db
VOLUME /data
EXPOSE 8080 9999/udp

CMD ["python3", "-u", "powerwatch.py"]
