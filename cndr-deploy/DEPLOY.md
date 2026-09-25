# CNDR items indexer on Fly.io

One Fly machine runs:
- `monero-wallet-rpc` v0.18.5.1 (official release tarball, SHA256-pinned), bound to `127.0.0.1:28084`, no RPC login, with a throwaway **verifier** wallet in `/data/verifier/`. It only serves `check_tx_proof` / `check_reserve_proof` / `verify`; it holds no user keys and no funds. Created automatically on first boot, reused afterwards.
- `indexer.py serve --port 8787` (public, via Fly HTTPS). State in `/data/state.json`.

`monerod` is **not** in the image; point `CNDR_DAEMON` at one you run.

Layout:
```
Dockerfile        multi-stage: fetch+verify monero tarball -> python:3.12-slim
entrypoint.sh     starts wallet-rpc, opens/creates verifier wallet, starts indexer; exits 1 if either child dies
fly.toml          http_service :8787, https, volume cndr_data -> /data, check GET /state, auto-stop off
app/              copies of indexer.py + proofs.py (re-copy from cndr-items/ after changing them)
```

## Prerequisites
- `flyctl` installed (`curl -L https://fly.io/install.sh | sh`).
- Auth via env var only: `export FLY_API_TOKEN=...` in your own shell (create with `fly tokens create deploy` or `fly auth token`). Never paste the token into chat, commit it, or put it in `fly.toml`.
- A reachable `monerod` with RPC open (restricted mode is fine: indexer uses `get_info`, `/get_transactions`, `/is_key_image_spent`; wallet-rpc needs the standard wallet-sync RPCs). No RPC login support: the indexer's urllib client does not do digest auth.

## Deploy
Run from this directory. Pick your own app name/region.
```sh
cd cndr-deploy
fly launch --no-deploy --copy-config --name <app-name> --region iad
fly volumes create cndr_data --size 1 --region iad --app <app-name> --yes
fly secrets set CNDR_DAEMON=http://<monerod-host>:18081 --app <app-name> --stage
fly deploy --ha=false --app <app-name>
```
- `--copy-config` keeps this `fly.toml` (launch rewrites `app` to your name).
- `--ha=false` + a single volume = exactly one machine. Do not scale past 1: state is a local JSON file.
- If the network is not mainnet, also: `fly secrets set CNDR_NETWORK=stagenet` (or `testnet`) or edit `[env]` in `fly.toml`. A `monerod --regtest` node uses mainnet address prefixes: keep `mainnet`.

## Pointing at a monerod
`CNDR_DAEMON` is a URL: `http://host:port` or `https://host[:port]` (no trailing path). The entrypoint converts it to wallet-rpc's `--daemon-address host:port --daemon-ssl enabled|disabled --untrusted-daemon`; the indexer uses the URL directly.
- Public node / your VPS: `fly secrets set CNDR_DAEMON=http://node.example.com:18081` (or 18089 for restricted public RPC). Firewall it to Fly egress if you care.
- monerod as another Fly app in the same org: `CNDR_DAEMON=http://<monerod-app>.internal:18081` (monerod must bind `--rpc-bind-ip ::` / `0.0.0.0` with `--confirm-external-bind`).
- Changing it: `fly secrets set CNDR_DAEMON=...` restarts the machine.

Optional env: `CNDR_SYNC_SECS` (default 20), `VERIFIER_WALLET_PASSWORD` (secret; default empty; must stay the same once the wallet exists).

## Verify after deploy
```sh
fly status --app <app-name>                  # 1 machine, started, check passing
fly logs --app <app-name>                    # expect: "verifier wallet ready: 4..." then "cndr indexer on :8787"
curl -fsS https://<app-name>.fly.dev/state   # {"collections": {}, "items": {}, "events": []} on a fresh volume
```
`sync error: ... Connection refused` in logs = `CNDR_DAEMON` unreachable. `/state` still returns 200 (it only reads the file); once the daemon is reachable, `synced_height` appears in `/state` within `CNDR_SYNC_SECS`.

## Local test (optional)
```sh
docker build -t cndr-indexer:local .
docker run --rm -p 8787:8787 -v cndr-data:/data -e CNDR_DAEMON=http://host.docker.internal:18081 cndr-indexer:local
curl localhost:8787/state
```

## Upgrading Monero
Edit `MONERO_VERSION` and both `MONERO_SHA256_*` args in the Dockerfile from the GPG-signed https://www.getmonero.org/downloads/hashes.txt, then `fly deploy`.
