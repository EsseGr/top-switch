FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN grep -ivE '^(pytest|#)' requirements.txt > requirements-runtime.txt \
    && pip install --no-cache-dir -r requirements-runtime.txt
COPY top_switch ./top_switch
COPY skins ./skins
ENV TOP_SWITCH_CONFIG=/data/config.yaml
VOLUME /data
EXPOSE 8000
USER 1000:1000
CMD ["python", "-m", "top_switch"]
