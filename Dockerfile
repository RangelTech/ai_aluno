FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 -m app && mkdir /data && chown app /data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER app
EXPOSE 8080
VOLUME /data
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/health',timeout=3)"
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080","--workers","1","--proxy-headers","--forwarded-allow-ips","*"]
