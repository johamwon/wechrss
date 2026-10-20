FROM python:3.12-slim
LABEL org.opencontainers.image.title="WeRSS" \
      org.opencontainers.image.description="Self-hosted WeChat public account to RSS service" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium
COPY . .
RUN mkdir -p /app/data
EXPOSE 8080 7860
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8080'), timeout=5)" || exit 1
CMD ["sh", "-c", "python web_app.py --host 0.0.0.0 --port ${PORT:-8080}"]
