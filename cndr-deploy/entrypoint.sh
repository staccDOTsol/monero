#!/usr/bin/env bash
# Start keyless verifier wallet-rpc (loopback only), ensure verifier wallet exists
# on the volume, then start the indexer. If either process exits, the container
# exits so Fly restarts the machine.
set -euo pipefail

: "${CNDR_DAEMON:?CNDR_DAEMON must be set (monerod RPC base URL, e.g. http://node.example:18081)}"
DATA_DIR="${DATA_DIR:-/data}"
VERIFIER_PORT="${VERIFIER_PORT:-28084}"
PORT="${PORT:-8787}"
CNDR_NETWORK="${CNDR_NETWORK:-mainnet}"
WALLET_DIR="$DATA_DIR/verifier"
WALLET_NAME="${VERIFIER_WALLET_NAME:-verifier}"
WALLET_PASSWORD="${VERIFIER_WALLET_PASSWORD:-}"

export CNDR_STATE="${CNDR_STATE:-$DATA_DIR/state.json}"
export CNDR_VERIFIER="http://127.0.0.1:${VERIFIER_PORT}/json_rpc"
export CNDR_DAEMON="${CNDR_DAEMON%/}"

mkdir -p "$WALLET_DIR" "$(dirname "$CNDR_STATE")"

# wallet-rpc wants host:port plus an explicit ssl flag, not a URL.
case "$CNDR_DAEMON" in
  https://*) daemon_hostport="${CNDR_DAEMON#https://}"; daemon_ssl=enabled ;;
  http://*)  daemon_hostport="${CNDR_DAEMON#http://}";  daemon_ssl=disabled ;;
  *) echo "CNDR_DAEMON must start with http:// or https://" >&2; exit 1 ;;
esac
daemon_hostport="${daemon_hostport%%/*}"
case "$daemon_hostport" in
  *:*) ;;
  *) if [ "$daemon_ssl" = enabled ]; then daemon_hostport="$daemon_hostport:443"; else daemon_hostport="$daemon_hostport:80"; fi ;;
esac

net_flag=()
case "$CNDR_NETWORK" in
  mainnet) ;;  # also correct for monerod --regtest (mainnet address prefixes)
  stagenet) net_flag=(--stagenet) ;;
  testnet)  net_flag=(--testnet) ;;
  *) echo "CNDR_NETWORK must be mainnet|stagenet|testnet" >&2; exit 1 ;;
esac

monero-wallet-rpc "${net_flag[@]}" \
  --non-interactive \
  --rpc-bind-ip 127.0.0.1 --rpc-bind-port "$VERIFIER_PORT" \
  --disable-rpc-login \
  --wallet-dir "$WALLET_DIR" \
  --daemon-address "$daemon_hostport" --daemon-ssl "$daemon_ssl" --untrusted-daemon \
  --log-file "$WALLET_DIR/wallet-rpc.log" --max-log-files 2 --log-level 0 &
wallet_pid=$!

# Wait for wallet-rpc, then open the verifier wallet (create it on first boot).
WALLET_NAME="$WALLET_NAME" WALLET_PASSWORD="$WALLET_PASSWORD" python3 - <<'PY'
import json, os, sys, time, urllib.request
url = os.environ["CNDR_VERIFIER"]
def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())
for _ in range(120):
    try:
        rpc("get_version"); break
    except Exception:
        time.sleep(1)
else:
    sys.exit("wallet-rpc did not come up within 120s")
name, pw = os.environ["WALLET_NAME"], os.environ["WALLET_PASSWORD"]
out = rpc("open_wallet", {"filename": name, "password": pw})
if "error" in out:
    print("verifier wallet not openable (%s); creating" % out["error"].get("message"), flush=True)
    out = rpc("create_wallet", {"filename": name, "password": pw, "language": "English"})
    if "error" in out:
        sys.exit("create_wallet failed: %s" % out["error"])
print("verifier wallet ready:", rpc("get_address", {"account_index": 0})["result"]["address"], flush=True)
PY

python3 /app/indexer.py serve --port "$PORT" &
indexer_pid=$!

stopping=0
trap 'stopping=1; kill -TERM "$indexer_pid" "$wallet_pid" 2>/dev/null || true' TERM INT
status=0
wait -n "$indexer_pid" "$wallet_pid" || status=$?
kill -TERM "$indexer_pid" "$wallet_pid" 2>/dev/null || true
wait || true
if [ "$stopping" = 1 ]; then exit 0; fi
# Unexpected child exit: fail non-zero so Fly's restart policy kicks in.
echo "a child process exited unexpectedly (status $status); exiting" >&2
exit 1
