#!/bin/sh
# {{NAME}} ({{TICKER}}) seed node: full node, public P2P, restricted public RPC.
# Extra monerod flags can be passed via MONEROD_ARGS (e.g. --add-priority-node=ip:port).
set -eu
mkdir -p "$DATA_DIR"
exec monerod \
  --non-interactive \
  --data-dir "$DATA_DIR" \
  --log-file "$DATA_DIR/monerod.log" --max-log-files 2 --log-level "${LOG_LEVEL:-0}" \
  --p2p-bind-ip 0.0.0.0 --p2p-bind-port "$P2P_PORT" \
  --rpc-bind-ip 0.0.0.0 --rpc-bind-port "$RPC_PORT" --restricted-rpc \
  --public-node --confirm-external-bind \
  --no-zmq --no-igd \
  --disable-dns-checkpoints --check-updates disabled \
  --out-peers 32 --in-peers 128 \
  ${MONEROD_ARGS:-}
