FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY publish.sh /publish.sh
RUN chmod +x /publish.sh
ENTRYPOINT ["/publish.sh"]
