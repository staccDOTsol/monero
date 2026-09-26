# {{NAME}} ({{TICKER}}) public seed node: monerod built from this chain branch.
# Build context is the repo root of branch chain/{{TICKER}} (see /fly.toml).
# Submodules (external/*) must be checked out before `fly deploy`.

FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config git ca-certificates \
      libboost-chrono-dev libboost-date-time-dev libboost-filesystem-dev libboost-locale-dev \
      libboost-program-options-dev libboost-regex-dev libboost-serialization-dev \
      libboost-system-dev libboost-thread-dev \
      libssl-dev libzmq3-dev libunbound-dev libsodium-dev libunwind8-dev liblzma-dev \
      libreadline-dev libexpat1-dev libhidapi-dev libusb-1.0-0-dev libprotobuf-dev \
      protobuf-compiler libudev-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN test -f external/randomx/CMakeLists.txt || { echo "run: git submodule update --init --recursive" >&2; exit 1; }
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DUSE_DEVICE_TREZOR=OFF -DMANUAL_SUBMODULES=1 \
 && cmake --build build --target daemon --parallel "$(nproc)" \
 && strip build/bin/monerod

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl libboost-chrono1.74.0 libboost-date-time1.74.0 libboost-filesystem1.74.0 \
      libboost-locale1.74.0 libboost-program-options1.74.0 libboost-regex1.74.0 \
      libboost-serialization1.74.0 libboost-system1.74.0 libboost-thread1.74.0 \
      libssl3 libzmq5 libunbound8 libsodium23 libunwind8 liblzma5 libreadline8 libexpat1 \
      libhidapi-libusb0 libusb-1.0-0 libprotobuf32 libudev1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/monerod /usr/local/bin/monerod
COPY xmrfun/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY xmrfun/chain.json /etc/xmrfun/chain.json
RUN /usr/local/bin/monerod --version
ENV DATA_DIR=/data P2P_PORT={{P2P_PORT}} RPC_PORT={{RPC_PORT}}
EXPOSE {{P2P_PORT}} {{RPC_PORT}}
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
