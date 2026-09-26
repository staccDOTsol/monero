#!/usr/bin/env bash
# Build monerod, monero-wallet-cli and monero-wallet-rpc for chain/<TICKER> into
# out/<TICKER>/, then check the built daemon produces the genesis block hash
# mkchain.py computed. macOS/Homebrew defaults; override CMAKE_PREFIX_PATH/JOBS.
set -euo pipefail

TICKER="${1:?usage: build.sh <TICKER>}"
TICKER="$(echo "$TICKER" | tr a-z A-Z)"
HERE="$(cd "$(dirname "$0")" && pwd)"
JSON="$HERE/chains/$TICKER.json"
[ -f "$JSON" ] || { echo "no $JSON; run mkchain.py first" >&2; exit 1; }
jget() { python3 -c "import json,sys; d=json.load(open('$JSON')); print(eval(sys.argv[1]))" "$1"; }

WT="$(jget 'd["worktree"]')"
OUT="$HERE/out/$TICKER"
BUILD="$WT/build/release"
export CMAKE_PREFIX_PATH="${CMAKE_PREFIX_PATH:-/opt/homebrew/opt/expat;/opt/homebrew/opt/openssl@3}"

git -C "$WT" submodule update --init --recursive
cmake -S "$WT" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DUSE_DEVICE_TREZOR=OFF
make -C "$BUILD" -j"${JOBS:-12}" daemon simplewallet wallet_rpc_server

mkdir -p "$OUT"
cp "$BUILD"/bin/{monerod,monero-wallet-cli,monero-wallet-rpc} "$OUT/"
cp "$JSON" "$OUT/chain.json"

# Genesis check: start the new daemon offline on a scratch dir and ask it for block 0.
TMP="$(mktemp -d)"
RPC=$(( $(jget 'd["ports"]["rpc"]') + 100 ))
"$OUT/monerod" --data-dir "$TMP" --offline --no-zmq --non-interactive --log-file "$TMP/log" \
  --p2p-bind-port $((RPC + 1)) --rpc-bind-ip 127.0.0.1 --rpc-bind-port "$RPC" >/dev/null &
PID=$!
trap 'kill $PID 2>/dev/null; wait $PID 2>/dev/null; rm -rf "$TMP"' EXIT
GOT=""
for _ in $(seq 60); do
  GOT="$(curl -s "http://127.0.0.1:$RPC/json_rpc" -d '{"jsonrpc":"2.0","id":0,"method":"get_block_header_by_height","params":{"height":0}}' \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["block_header"]["hash"])' 2>/dev/null || true)"
  [ -n "$GOT" ] && break
  sleep 1
done
WANT="$(jget 'd["genesis"]["block_hash"]')"
if [ "$GOT" != "$WANT" ]; then
  echo "GENESIS MISMATCH: daemon=$GOT mkchain=$WANT" >&2
  exit 1
fi
echo "built $OUT ($(ls "$OUT" | tr '\n' ' '))"
echo "genesis ok: $GOT"
