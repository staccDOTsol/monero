FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/* \
 && curl -L https://fly.io/install.sh | FLYCTL_INSTALL=/usr/local sh
WORKDIR /app
COPY launch.py chainconfig.py cnutil.py fly.chain.toml ./
ENV PYTHONUNBUFFERED=1 CHAINS_DIR=/data/chains
CMD ["python", "launch.py"]
