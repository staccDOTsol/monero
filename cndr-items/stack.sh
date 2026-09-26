#!/usr/bin/env bash
# One-command CNDR Items stack.
#
#   stack.sh regtest   throwaway local chain + wallets A/B + verifier + indexer, then runs e2e.py
#   stack.sh mainnet   real Monero: verifier + your wallet-rpc + indexer against a public node
#   stack.sh down      stop everything this script started
#
# Uses the stock Monero binaries on PATH (brew install monero), NOT the Cinderfork build:
# real XMR needs stock address prefixes.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="${CNDR_RUN_DIR:-$HOME/.cndr-stack}"
MAINNET_NODE="${CNDR_MAINNET_NODE:-node.sethforprivacy.com:18089}"
USER_WALLET="${CNDR_USER_WALLET:-$HOME/xmr-wallets/otc}"
PY="${PYTHON:-python3}"
mkdir -p "$RUN"

bg() {  # bg <name> <cmd...>: start in background, record pid, log to $RUN/<name>.log
  local name=$1; shift
  "$@" >"$RUN/$name.log" 2>&1 &
  echo $! >"$RUN/$name.pid"
}

wait_rpc() {  # wait_rpc <json_rpc url> <method>
  for _ in $(seq 1 120); do
    curl -sf -m 3 "$1" -d "{\"jsonrpc\":\"2.0\",\"id\":\"0\",\"method\":\"$2\"}" >/dev/null && return 0
    sleep 1
  done
  echo "timed out waiting for $1" >&2; return 1
}

rpc() {  # rpc <url> <method> [params-json]
  curl -sf -m 120 "$1" -d "{\"jsonrpc\":\"2.0\",\"id\":\"0\",\"method\":\"$2\",\"params\":${3:-{\}}}"
}

down() {
  for f in "$RUN"/*.pid; do
    [ -e "$f" ] || continue
    kill "$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  done
  echo "stack stopped"
}

wallet_rpc() {  # wallet_rpc <name> <port> <daemon host:port> [extra args...]
  local name=$1 port=$2 daemon=$3; shift 3
  bg "$name" monero-wallet-rpc --non-interactive --rpc-bind-ip 127.0.0.1 --rpc-bind-port "$port" \
    --disable-rpc-login --daemon-address "$daemon" --log-file "$RUN/$name.wallet.log" "$@"
  wait_rpc "http://127.0.0.1:$port/json_rpc" get_version
}

regtest() {
  down >/dev/null
  local R="$RUN/regtest"; rm -rf "$R"; mkdir -p "$R/wallets"
  bg regtest-daemon monerod --regtest --offline --fixed-difficulty 1 --non-interactive \
    --data-dir "$R/chain" --rpc-bind-port 28081 --p2p-bind-port 28080 --no-zmq --log-file "$R/monerod.log"
  wait_rpc http://127.0.0.1:28081/json_rpc get_info
  for w in a:28082 b:28083 verifier:28084; do
    wallet_rpc "regtest-${w%%:*}" "${w##*:}" 127.0.0.1:28081 --wallet-dir "$R/wallets" --allow-mismatched-daemon-version --trusted-daemon
    rpc "http://127.0.0.1:${w##*:}/json_rpc" create_wallet "{\"filename\":\"${w%%:*}\",\"password\":\"\",\"language\":\"English\"}" >/dev/null
  done
  # Fund A: 80 blocks to A, then 60 more so the coinbase unlocks.
  local a_addr; a_addr=$(rpc http://127.0.0.1:28082/json_rpc get_address '{"account_index":0}' | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["result"]["address"])')
  rpc http://127.0.0.1:28081/json_rpc generateblocks "{\"amount_of_blocks\":140,\"wallet_address\":\"$a_addr\"}" >/dev/null
  for _ in $(seq 1 60); do  # wait until wallet A actually sees spendable coins
    rpc http://127.0.0.1:28082/json_rpc refresh >/dev/null || true
    rpc http://127.0.0.1:28082/json_rpc get_balance | grep -q '"unlocked_balance": [1-9]' && break
    sleep 1
  done

  CNDR_DAEMON=http://127.0.0.1:28081 CNDR_VERIFIER=http://127.0.0.1:28084/json_rpc \
  CNDR_STATE="$R/state.json" CNDR_SYNC_SECS=2 CNDR_ITEM_UNIT=10000000000 bg regtest-indexer "$PY" "$HERE/indexer.py" serve --port 8797
  sleep 2
  CNDR_ITEM_UNIT=10000000000 E2E_INDEXER=http://127.0.0.1:8797 "$PY" "$HERE/e2e.py"
}

mainnet() {
  local M="$RUN/mainnet"; mkdir -p "$M/verifier"
  local node_url="http://$MAINNET_NODE"
  # Keyless verifier: never holds funds, only checks proofs.
  wallet_rpc mainnet-verifier 18084 "$MAINNET_NODE" --wallet-dir "$M/verifier" --untrusted-daemon
  if ! rpc http://127.0.0.1:18084/json_rpc open_wallet '{"filename":"verifier","password":""}' | grep -q '"result"'; then
    rpc http://127.0.0.1:18084/json_rpc create_wallet '{"filename":"verifier","password":"","language":"English"}' >/dev/null
  fi
  # Your wallet, for cndr-wallet. Loopback only.
  if [ -e "$USER_WALLET.keys" ]; then
    wallet_rpc mainnet-user 18082 "$MAINNET_NODE" --wallet-file "$USER_WALLET" \
      --password-file "$USER_WALLET.password" --untrusted-daemon
    echo "your wallet-rpc: http://127.0.0.1:18082  ($(rpc http://127.0.0.1:18082/json_rpc get_address '{"account_index":0}' | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["result"]["address"])'))"
  else
    echo "no wallet at $USER_WALLET(.keys); skipping user wallet-rpc" >&2
  fi
  CNDR_DAEMON="$node_url" CNDR_VERIFIER=http://127.0.0.1:18084/json_rpc CNDR_STATE="$M/state.json" \
    bg mainnet-indexer "$PY" "$HERE/indexer.py" serve --port 8787
  sleep 2
  curl -sf http://127.0.0.1:8787/state >/dev/null && echo "indexer: http://127.0.0.1:8787  (node $MAINNET_NODE)"
  echo "logs: $RUN/*.log   stop: $0 down"
}

case "${1:-}" in
  regtest) regtest ;;
  mainnet) mainnet ;;
  down) down ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
