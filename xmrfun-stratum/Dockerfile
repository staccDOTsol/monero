# xmrfun multi-chain stratum
# Build context: this directory (xmrfun-stratum/).
#   docker build -t xmrfun-stratum .
#   docker run -p 3333:3333 -p 8080:8080 -v $PWD/chains.json:/app/chains.json xmrfun-stratum

# ---- librandomx (shared) built from the same RandomX commit as external/randomx
FROM debian:bookworm-slim AS randomx
ARG RANDOMX_REPO=https://github.com/tevador/RandomX
ARG RANDOMX_REF=12f2c2ffe2108d6cf54c391fee33c8bc3646cdab
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git cmake make g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q randomx && cd randomx && git remote add origin "$RANDOMX_REPO" \
 && git fetch -q --depth 1 origin "$RANDOMX_REF" && git checkout -q FETCH_HEAD
RUN cmake -S randomx -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
      -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
 && cmake --build build --target randomx -j"$(nproc)" \
 && mkdir -p /out && cp -P build/librandomx.so* /out/

# ---- runtime: stdlib-only Python + librandomx
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --home /app --shell /usr/sbin/nologin pool \
 && mkdir -p /data && chown pool /data
WORKDIR /app
COPY --from=randomx /out/ /app/lib/
COPY xmrfun_stratum/ /app/xmrfun_stratum/
COPY chains.json /app/chains.json
ENV RANDOMX_LIB=/app/lib/librandomx.so \
    PYTHONUNBUFFERED=1 \
    XMRFUN_CONFIG=/app/chains.json
# Fly volumes mount root-owned; stay root so the ledger on /data is writable.
EXPOSE 3333 8080
# self-test: RandomX reference vector must match before we accept shares
RUN python -c "from xmrfun_stratum.randomx import RandomX; \
assert RandomX().hash(b'test key 000', b'This is a test').hex() == \
'639183aae1bf4c9a35884cb46b09cad9175f04efd7684e7262a0ac1c2f0b4e3f'; print('randomx ok')"
CMD ["python", "-m", "xmrfun_stratum"]
