# xmrfun-stratum

One stratum endpoint for many RandomX CryptoNote chains. Miners point xmrig at
a single URL. The pool splits their hashrate across every launched xmrfun chain,
and optionally Monero mainnet, in proportion to each chain's **profitability**:

    EV per hash (in XMR) = block_reward / network_difficulty * price_in_XMR

The code is Python 3 asyncio and uses only the standard library. The only native
piece is `librandomx`, loaded through ctypes and used to verify shares.

```
            xmrig ... xmrig                     (one URL: pool:3333)
                 \   |   /
            +------------------+   GET /stats  /health  /ledger (:8080)
            |  stratum server  |
            |  vardiff, jobs   |---- sqlite ledger (shares / blocks / events)
            |  share verify    |---- librandomx (light mode, thread pool)
            |  allocator loop  |
            +------------------+
             /       |        \        get_block_template(reserve_size=8)
        monerod   monerod   monerod    submit_block
        chain A   chain B   XMR mainnet (optional)
```

## Running it

```sh
git submodule update --init external/randomx     # from the repo root
./build_randomx.sh                               # -> lib/librandomx.{so,dylib}
python3 -m xmrfun_stratum --config chains.json   # stratum :3333, stats :8080
xmrig -o 127.0.0.1:3333 -u <address>[.worker][+fixeddiff] -a rx/0 -k
python3 -m unittest discover -s tests
scripts/demo_regtest.py                          # full end-to-end check, needs monerod + xmrig
```

## Config: `chains.json`

The `pool` section is optional. Every key has a default, listed in `DEFAULTS` in
`xmrfun_stratum/pool.py`. Each entry in `chains` looks like this:

| key | meaning |
|---|---|
| `name`, `ticker` | Identity. `name` is the key used in the ledger and stats. |
| `daemon` | monerod RPC URL. Optional `rpc_login` (`user:pass`) uses digest auth. |
| `address` | Pool payout address on that chain. Coinbase outputs go here; miners are paid later from the ledger. |
| `price` | The coin's price in XMR. See the price sources below. |
| `enabled` | `false` keeps the chain configured but gives it no work. It is not polled. |
| `atomic_units` | Atomic units per coin. Default `1e12`. |

**Price sources** are pluggable. Register a new one with `@prices.register("type")`.
- `{"type":"static","value":0.02}`, or just the number `0.02`.
- `{"type":"http","url":"https://…/price?ticker=FOO","field":"price_xmr","ttl":30,"max_age":300}`.
  The pool fetches JSON from the URL and reads the dotted `field` path. A bare JSON
  number also works. Any URL scheme urllib supports is accepted, including `file://`.
  After a failed fetch, the last good price is used for `max_age` seconds. After
  that the chain is treated as unpriced and gets weight 0. Use this source for
  the xmrfun OTC market feed.

**Hot reload.** The pool re-reads the config file when its mtime changes, checking
every 2 s. You can change prices, enable or disable chains, add new chains, and
adjust allocator, vardiff and verify settings without dropping any miner. When a
chain's price spec changes, its old profitability samples are discarded, so the
newest data wins. Listen ports and the ledger path only change on restart.

## Design

### Work generation (`cnblob.py`, `chain.py`)
- Every chain has a poller that calls `/get_height` every `poll_interval`. It calls
  `get_block_template` with `reserve_size=8` when the tip changes, and at least
  every `template_refresh` seconds so that new mempool transactions get included.
- Every job a miner receives is unique. The pool writes a fresh 8-byte extra-nonce
  into the coinbase reserve and rebuilds the hashing blob:
  `header || tree_hash(miner_tx_hash, tx_hashes…) || varint(n)`. The Merkle
  branch for leaf 0 is computed once per template. After that, each job costs one
  coinbase-prefix Keccak plus about log2(n) hashes, roughly 1 ms in pure Python.
- Keccak-256 is a small pure-Python implementation. hashlib's `sha3_256` uses
  different padding, so it can't be used. If pycryptodome is installed, the pool
  uses it instead.
- **Self-check.** For each new template, the pool rebuilds the hashing blob from the
  unmodified template and compares it byte for byte with monerod's
  `blockhashing_blob`. A fork that changes the block or coinbase format fails this
  check. That chain is then marked unhealthy and gets no work, so the pool never
  hands out bad jobs.
- Jobs follow the xmrig Monero pool protocol: `{blob, job_id, target (64-bit LE),
  algo:"rx/0", height, seed_hash}`. The pool pushes a new job whenever a chain
  switch, a new template or a vardiff change happens.

### Share validation (`randomx.py`): trust-but-verify
Cheap checks run on every share: the job exists and is current (`Block expired`
when the tip has moved), the nonce isn't a duplicate, and the claimed hash meets
the share target.

RandomX checks use light mode, which needs a 256 MiB cache per seed and takes
about 20 ms per hash per thread on an M-series core. They run on a thread pool
(`verify.threads`) and never block the event loop.
- **Block candidates are always fully verified.** A candidate is any share whose
  claimed hash meets the network difficulty.
- The **first `first_n` shares** from each new connection are fully verified.
- After that, a random **`ratio`** of ordinary shares is verified. Set it to `1.0`
  to verify everything.
- **On a hash mismatch** the share is rejected, the miner goes back to full
  verification, and an `invalid_share` event is written to the ledger. After
  `ban_after_invalid` mismatches the IP is banned for `ban_seconds`.
- **Backpressure.** When more than `max_pending` verifications are queued, sampled
  checks are skipped and counted in `verify_skipped`. Block candidates are never
  skipped.

If `librandomx` can't be loaded, the pool logs a warning and trusts share hashes;
monerod still validates any block candidate. To refuse to start in that case, set
`verify.require_randomx`.

### Allocator (`allocator.py`)
Every `interval` seconds:
1. Each usable chain is sampled for EV per hash. Its **score** is the mean over the
   last `profit_window` seconds. A chain with a stale template, a dead daemon, no
   price, or `enabled: false` gets no score.
2. Target weights are `w_c = score_c^power / Σ score^power`. Chains below
   `min_weight` are dropped. `power = 1` gives proportional allocation.
3. Each chain gets an error term, measured in H/s:
   `e_c = (R_c − w_c·R_tot) + clamp((W_c − w_c·W_tot)/repay_time, ±g)`.
   - `R` is the estimated hashrate currently assigned to the chain.
   - `W` is the accepted share difficulty over `work_window`.
   - `g` is the largest single miner's hashrate.

   With many small miners this splits hashrate proportionally. When hashrate is too
   lumpy to split exactly, such as one big rig or 3 rigs across 2 chains, the
   bounded work-debt term makes miners **time-slice** so that delivered work still
   converges to the weights.
4. Miners are moved greedily. A miner is only moved once it has been on its chain
   for `min_dwell`, and only if the move cuts Σ|e| by more than `hysteresis` × its
   own hashrate. Together these two rules give the anti-thrash hysteresis. A miner
   on a chain that died is moved immediately.

Each miner's hashrate is estimated from its accepted share difficulty over
`vardiff.hashrate_window`. New miners start from a prior of `diff/target_time`.
New connections are placed on the chain where they reduce the error the most.

### Vardiff
The pool aims for one share per `target_time` seconds per miner. It retargets every
`retarget_time` seconds when the average share time is off by more than
`variance`, changing difficulty by at most `max_step`× per step. A miner that
submits no shares for 2 × `retarget_time` has its difficulty halved. Share
difficulty is capped at the network difficulty. Appending `+N` to the login fixes
the difficulty at N.

### Accounting (`ledger.py`)
The ledger is a SQLite file (`ledger_path`, in WAL mode):
- `shares(ts, miner, worker, chain, height, difficulty, verified)`: one row per
  accepted share, weighted by the difficulty it was accepted at.
  `Ledger.pplns_window(chain, N)` returns the per-miner work in the last N units on
  a chain, which is the input to PPLNS.
- `blocks(…, hash, reward, status, detail)`: every submitted candidate and
  monerod's verdict.
- `events`: invalid shares, bans, chain switches and config reloads.

### HTTP
- `GET /stats` returns pool totals and the verification policy. For each chain it
  gives height, difficulty, reward, price, EV per hash, windowed score, target
  weight, hashrate share, work share, shares and blocks found. It also lists each
  miner (chain, hashrate, difficulty, accepted/rejected/verified counts, switches),
  the recent allocation switches, and recent blocks. Miner IP addresses aren't
  exposed.
- `GET /health` returns 200 if any chain is usable.
- `GET /ledger` returns share totals per chain and miner.

## Fly.io
`Dockerfile` builds `librandomx` from the same RandomX commit as the
`external/randomx` submodule and checks the RandomX reference vector at build time.
The runtime image is `python:3.12-slim`. `fly.toml` sets up a raw TCP service on
port 3333 for stratum (use a dedicated IPv4), HTTP on 80/443 for the stats API, and
a volume at `/data` for the ledger. It has not been deployed.

## Known limitations
- The pool doesn't pay out yet. It records PPLNS inputs; wallet and payout logic
  come later. The coinbase goes to the pool address on each chain.
- It can't detect block withholding, where a miner keeps back shares that would
  have been blocks. That's inherent to pooled mining without extra protocol work.
- When chains use different RandomX seeds (each xmrfun chain has its own genesis),
  every switch makes xmrig rebuild its cache or dataset: about 0.5 s in light mode,
  several seconds in fast mode. `min_dwell` keeps this cost small. The allocator
  doesn't yet group chains that share a seed.
- The block parser supports the Monero v2 coinbase (`txout_to_key` or
  `txout_to_tagged_key`, RCTTypeNull) and v1. Other formats fail the self-check and
  the chain is disabled.
- It runs as a single process with in-memory miner state. Stratum has no TLS and
  no NiceHash nonce-splitting mode.
- Profitability doesn't model the pool's own effect on a chain's difficulty. That
  effect only shows up through the rolling window, as difficulty responds.
