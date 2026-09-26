#!/usr/bin/env bash
# Start / stop two stock monerod regtest daemons used as two "chains" for the
# local end-to-end test.  Ports are chosen away from the 18xxx/28xxx defaults so
# they don't collide with other local stacks.
#
#   scripts/regtest_daemons.sh start <base_dir> [fixed_difficulty]
#   scripts/regtest_daemons.sh stop  <base_dir>
#   scripts/regtest_daemons.sh info
set -euo pipefail
cmd="${1:-}"; base="${2:-./data/regtest}"; diff="${3:-5000}"
CHAINS=("alpha 48081 48080" "beta 48091 48090")

case "$cmd" in
  start)
    for c in "${CHAINS[@]}"; do
      set -- $c
      mkdir -p "$base/$1"
      monerod --regtest --offline --fixed-difficulty "$diff" \
        --data-dir "$base/$1" --rpc-bind-ip 127.0.0.1 --rpc-bind-port "$2" \
        --p2p-bind-ip 127.0.0.1 --p2p-bind-port "$3" --no-zmq --non-interactive \
        --log-file "$base/$1/monerod.log" --detach --pidfile "$base/$1/monerod.pid" >/dev/null
      echo "started $1 rpc=$2 pid=$(cat "$base/$1/monerod.pid" 2>/dev/null || echo '?')"
    done
    ;;
  stop)
    for c in "${CHAINS[@]}"; do
      set -- $c
      if [ -f "$base/$1/monerod.pid" ]; then
        pid="$(cat "$base/$1/monerod.pid")"
        kill "$pid" 2>/dev/null && echo "stopped $1 (pid $pid)" || true
        rm -f "$base/$1/monerod.pid"
      fi
    done
    ;;
  info)
    for c in "${CHAINS[@]}"; do
      set -- $c
      printf '%s ' "$1"
      curl -s "http://127.0.0.1:$2/get_info" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print("height=%d difficulty=%d top=%s" % (d["height"], d["difficulty"], d["top_block_hash"][:16]))' \
        || echo "unreachable"
    done
    ;;
  *) echo "usage: $0 start|stop|info <base_dir> [fixed_difficulty]" >&2; exit 2 ;;
esac
