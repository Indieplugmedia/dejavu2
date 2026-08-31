FROM --platform=linux/amd64 addictedcs/soundfingerprinting.emy:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh
EXPOSE 3340
ENTRYPOINT ["/app/start.sh"]
