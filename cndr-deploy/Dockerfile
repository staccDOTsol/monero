# CNDR items indexer + keyless monero-wallet-rpc verifier, one Fly machine.
# monerod is external (CNDR_DAEMON).

ARG MONERO_VERSION=v0.18.5.1

FROM debian:bookworm-slim AS monero
ARG MONERO_VERSION
ARG TARGETARCH
# SHA256 from https://www.getmonero.org/downloads/hashes.txt (GPG-signed by binaryFate)
ARG MONERO_SHA256_AMD64=22a7dda7b0cb699fdd6b7674c3b4a4465b337cc98a54983523b759e1e7cc9958
ARG MONERO_SHA256_ARM64=c0caf042cb7c7b760f5ad6be188084b59352440b32990a78b8051497b9398dbc
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl bzip2 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/monero
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x64;   sha="$MONERO_SHA256_AMD64" ;; \
      arm64) arch=armv8; sha="$MONERO_SHA256_ARM64" ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    tarball="monero-linux-${arch}-${MONERO_VERSION}.tar.bz2"; \
    curl -fsSLo "$tarball" "https://downloads.getmonero.org/cli/${tarball}"; \
    echo "${sha}  ${tarball}" | sha256sum -c -; \
    tar -xjf "$tarball"; \
    install -m 0755 monero-*-"${MONERO_VERSION}"/monero-wallet-rpc /usr/local/bin/monero-wallet-rpc; \
    /usr/local/bin/monero-wallet-rpc --version

FROM python:3.12-slim-bookworm
COPY --from=monero /usr/local/bin/monero-wallet-rpc /usr/local/bin/monero-wallet-rpc
WORKDIR /app
COPY app/indexer.py app/proofs.py ./
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CNDR_STATE=/data/state.json \
    CNDR_NETWORK=mainnet \
    VERIFIER_PORT=28084 \
    PORT=8787
EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
