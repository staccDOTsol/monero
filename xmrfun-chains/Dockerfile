# One image for every xmrfun chain: monerod + monero-wallet-rpc (chain identity comes from
# --chain-config at runtime) + xmrfun-genesis (chain config generator).
# Published once as registry.fly.io/xmrfun-chain:latest. From the repo root, with submodules
# checked out (git submodule update --init --recursive), on Fly's remote builder (linux/amd64):
#   fly apps create xmrfun-chain        # once; the app only hosts the image
#   fly deploy . -a xmrfun-chain -c xmrfun-chains/fly.chain.toml --dockerfile xmrfun-chains/Dockerfile \
#       --build-only --push --image-label latest
# Local alternative: docker build -f xmrfun-chains/Dockerfile -t xmrfun-chain .

FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config git ca-certificates \
      libboost-chrono-dev libboost-date-time-dev libboost-filesystem-dev libboost-locale-dev \
      libboost-program-options-dev libboost-regex-dev libboost-serialization-dev \
      libboost-system-dev libboost-thread-dev \
      libssl-dev libzmq3-dev libunbound-dev libsodium-dev libunwind8-dev liblzma-dev \
      libreadline-dev libexpat1-dev libhidapi-dev libusb-1.0-0-dev libprotobuf-dev \
      protobuf-compiler libudev-dev curl \
 && rm -rf /var/lib/apt/lists/*
# src/fcmp_pp/fcmp_pp_rust needs cargo (>= 1.85; bookworm's rustc is too old)
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH=/root/.cargo/bin:$PATH
WORKDIR /src
COPY . .
RUN test -f external/randomx/CMakeLists.txt || { echo "external/ is empty: git submodule update --init --recursive" >&2; exit 1; }
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DUSE_DEVICE_TREZOR=OFF -DMANUAL_SUBMODULES=1 \
 && cmake --build build --target daemon wallet_rpc_server --parallel "$(nproc)" \
 && strip build/bin/monerod build/bin/monero-wallet-rpc

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 \
      libboost-chrono1.74.0 libboost-date-time1.74.0 libboost-filesystem1.74.0 libboost-locale1.74.0 \
      libboost-program-options1.74.0 libboost-regex1.74.0 libboost-serialization1.74.0 \
      libboost-system1.74.0 libboost-thread1.74.0 \
      libssl3 libzmq5 libunbound8 libsodium23 libunwind8 liblzma5 libreadline8 libexpat1 \
      libhidapi-libusb0 libusb-1.0-0 libprotobuf32 libudev1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/monerod /src/build/bin/monero-wallet-rpc /usr/local/bin/
COPY xmrfun-chains/cnutil.py xmrfun-chains/chainconfig.py xmrfun-chains/xmrfun-genesis /opt/xmrfun/
COPY xmrfun-chains/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN ln -s /opt/xmrfun/xmrfun-genesis /usr/local/bin/xmrfun-genesis \
 && chmod 0755 /opt/xmrfun/xmrfun-genesis /usr/local/bin/entrypoint.sh \
 && monerod --version && xmrfun-genesis --name probe --ticker PROBE >/dev/null
ENV DATA_DIR=/data
EXPOSE 18080 18081 18083 18089
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
