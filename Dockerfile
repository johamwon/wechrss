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
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]
CMD ["python", "web_app.py", "--host", "0.0.0.0", "--port", "8080"]
