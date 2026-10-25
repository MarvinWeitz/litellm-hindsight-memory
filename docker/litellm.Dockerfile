FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir "litellm[proxy]==1.106.3" "hindsight-client==1.1.0" . \
    && pip install --no-cache-dir prisma

COPY config ./config
EXPOSE 4000
CMD ["litellm", "--config", "/app/config/litellm.yaml", "--port", "4000", "--host", "0.0.0.0"]
