#!/bin/sh
# xmrfun chain node (one Fly app per coin). CHAIN_CONFIG_JSON (base64 or raw JSON) ->
# $DATA_DIR/chain.json, then:
#   monerod            P2P            0.0.0.0:18080   public  (fly [[services]])
#                      RPC restricted 0.0.0.0:18089   public  (fly: https 443 + http 18089), CORS for the browser wallet
#                      RPC full       127.0.0.1 + [::]:18081  private network only (stratum: get_block_template/submit_block)
#   monero-wallet-rpc  pool wallet    127.0.0.1 + [::]:18083  private network only, no login
# The pool wallet is restored from POOL_WALLET_SEED (25 words, restore height 0) on first
# boot into $DATA_DIR/pool-wallet and reopened afterwards. Without the seed it is skipped.
# If monerod or wallet-rpc exits, the container exits so Fly restarts it.
set -eu
DATA_DIR="${DATA_DIR:-/data}"
CFG="$DATA_DIR/chain.json"
CORS="${RPC_CORS_ORIGINS:-https://xmrfun.xyz,https://xmrfun.fly.dev}"
P2P_PORT="${P2P_PORT:-18080}" RPC_PORT="${RPC_PORT:-18081}" PUBLIC_RPC_PORT="${PUBLIC_RPC_PORT:-18089}"
POOL_RPC_PORT="${POOL_RPC_PORT:-18083}" PRIVATE_BIND6="${PRIVATE_BIND6:-::}" PUBLIC_BIND="${PUBLIC_BIND:-0.0.0.0}"
mkdir -p "$DATA_DIR/pool-wallet"

if [ -n "${CHAIN_CONFIG_JSON:-}" ]; then
  case "$CHAIN_CONFIG_JSON" in
    \{*) printf '%s' "$CHAIN_CONFIG_JSON" > "$CFG.new" ;;
    *) printf '%s' "$CHAIN_CONFIG_JSON" | base64 -d > "$CFG.new" ;;
  esac
  # A data dir belongs to one chain: refuse a config for another one.
  if [ -s "$CFG" ] && ! python3 - "$CFG" "$CFG.new" <<'PY'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:])
keys = ("network_id", "genesis_tx", "genesis_nonce")
sys.exit(any(a.get(k) != b.get(k) for k in keys))
PY
  then
    echo "CHAIN_CONFIG_JSON is a different chain than the one in $DATA_DIR; refusing" >&2
    exit 1
  fi
  mv "$CFG.new" "$CFG"
fi
[ -s "$CFG" ] || { echo "set CHAIN_CONFIG_JSON (xmrfun-genesis output, base64 or raw)" >&2; exit 1; }

monerod --chain-config "$CFG" \
  --non-interactive \
  --data-dir "$DATA_DIR" \
  --log-file "$DATA_DIR/monerod.log" --max-log-files 2 --log-level "${LOG_LEVEL:-0}" \
  --p2p-bind-ip "$PUBLIC_BIND" --p2p-bind-port "$P2P_PORT" \
  --rpc-bind-ip 127.0.0.1 --rpc-bind-port "$RPC_PORT" \
  --rpc-use-ipv6 --rpc-bind-ipv6-address "$PRIVATE_BIND6" \
  --rpc-restricted-bind-ip "$PUBLIC_BIND" --rpc-restricted-bind-port "$PUBLIC_RPC_PORT" \
  --rpc-access-control-origins "$CORS" \
  --confirm-external-bind --public-node \
  --no-zmq --no-igd \
  --disable-dns-checkpoints --check-updates disabled \
  --out-peers 32 --in-peers 128 \
  ${MONEROD_ARGS:-} &
monerod_pid=$!

monero-wallet-rpc --chain-config "$CFG" \
  --non-interactive \
  --daemon-address "127.0.0.1:$RPC_PORT" --trusted-daemon \
  --rpc-bind-ip 127.0.0.1 --rpc-bind-port "$POOL_RPC_PORT" \
  --rpc-use-ipv6 --rpc-bind-ipv6-address "$PRIVATE_BIND6" \
  --disable-rpc-login --confirm-external-bind \
  --wallet-dir "$DATA_DIR/pool-wallet" \
  --log-file "$DATA_DIR/pool-wallet/wallet-rpc.log" --max-log-files 2 --log-level 0 &
wallet_pid=$!
trap 'kill -INT "$monerod_pid" "$wallet_pid" 2>/dev/null; wait; exit 0' INT TERM

# open the pool wallet, or restore it from the seed on first boot
POOL_RPC_PORT="$POOL_RPC_PORT" python3 - <<'PY' || echo "pool wallet not ready (see above); wallet-rpc keeps running" >&2
import json, os, sys, time, urllib.request
url = "http://127.0.0.1:%s/json_rpc" % os.environ["POOL_RPC_PORT"]
def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": method, "params": params or {}}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"}), timeout=600) as r:
        return json.loads(r.read())
for _ in range(120):
    try:
        rpc("get_version"); break
    except Exception:
        time.sleep(1)
else:
    sys.exit("wallet-rpc did not come up")
pw = os.environ.get("POOL_WALLET_PASSWORD", "")
if "error" not in rpc("open_wallet", {"filename": "pool", "password": pw}):
    print("pool wallet opened:", rpc("get_address")["result"]["address"], flush=True)
    sys.exit(0)
seed = os.environ.get("POOL_WALLET_SEED", "").strip()
if not seed:
    sys.exit("no pool wallet yet and POOL_WALLET_SEED is not set")
out = rpc("restore_deterministic_wallet", {"filename": "pool", "seed": seed, "password": pw,
                                           "restore_height": 0, "autosave_current": True})
if "error" in out:
    sys.exit("restore_deterministic_wallet failed: %s" % out["error"].get("message"))
print("pool wallet restored:", out["result"]["address"], flush=True)
PY

# exit (and let Fly restart us) as soon as either process dies
while kill -0 "$monerod_pid" 2>/dev/null && kill -0 "$wallet_pid" 2>/dev/null; do
  sleep 5
done
echo "monerod or wallet-rpc exited; stopping" >&2
kill -INT "$monerod_pid" "$wallet_pid" 2>/dev/null || true
wait || true
exit 1
