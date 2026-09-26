# xmrfun chain factory

Each xmrfun launch is its own RandomX CryptoNote chain, forked from this repo
(Monero + the Cinderfork changes). The factory turns launch params into a git
branch `chain/<TICKER>` that builds a working daemon and wallets.

```
python3 mkchain.py --name "Test Coin" --ticker TEST --block-time 60 \
    --premine 1000 --premine-address 4... \
    --seed 203.0.113.10 --seed seed.test.example.com
./build.sh TEST                       # -> out/TEST/{monerod,monero-wallet-cli,monero-wallet-rpc}
python3 verify.py TEST --blocks 10    # two local nodes, mine, sync, wallet check
```

| file | what |
|---|---|
| `mkchain.py` | derives params, creates worktree `work/<TICKER>` on branch `chain/<TICKER>`, patches, commits, writes `chains/<TICKER>.json` |
| `cnutil.py` | stdlib keccak / ed25519 / base58 / tx serialization; builds the genesis and premine txs (`python3 cnutil.py` self-tests against Monero's genesis hash) |
| `build.sh` | submodules + cmake + make, copies binaries to `out/<TICKER>/`, then checks the built daemon's block 0 hash equals the one mkchain computed |
| `verify.py` | local end-to-end run (real mainnet mode, not regtest) |
| `deploy/` | Dockerfile, entrypoint, fly.toml, .dockerignore templates rendered into each chain branch |

## Params

| flag | default | patched into |
|---|---|---|
| `--name`, `--ticker` | required | `CRYPTONOTE_NAME` = `xmrfun-<ticker>` (data dir `~/.xmrfun-<ticker>`), genesis tx extra |
| `--supply` (coins) | 2^64-1 atomic (Monero) | `MONEY_SUPPLY`; max 18446744 coins (12 decimals, uint64) |
| `--emission-speed` | 20 | `EMISSION_SPEED_FACTOR_PER_MINUTE` (lower = faster emission) |
| `--block-time` (s) | 120 | `DIFFICULTY_TARGET_V2`; multiple of 60 (consensus static_assert) |
| `--premine`, `--premine-address` | none | `PREMINE_TX`, `PREMINE_AMOUNT` (see below) |
| `--seed` (repeatable) | none | IPv4[:port] -> `get_ip_seed_nodes()`, hostnames -> `m_seed_nodes_list` (DNS, A records, default p2p port) |

Derived per chain:

* **Address prefixes** (deterministic from ticker). An address is base58 of
  `varint(prefix) || spend || view || checksum`, encoded in 8-byte blocks of 11
  chars. The first block is the prefix varint plus random key bytes, so the
  leading chars are fixed wherever the block with key bytes all `00` and all
  `ff` encode alike (`lead_chars()` in mkchain.py). Reachable first chars are
  `1`..`j`: 1-byte prefixes (<128) give `1`..`N` and nothing more than 1 char
  (Monero's 18 -> `4`); 2/3-byte varints give `N`..`j`, and their 2nd/3rd bytes
  steer the next 1-2 chars. mkchain searches all prefixes < 2^21 for the longest
  match with the ticker (max 3 chars) and picks three distinct ones (standard,
  integrated, subaddress), shuffled by a hash of the ticker. TEST gets
  `TESR...` / `TES...` / `TES9...`; DOGE only gets `D...`. Tickers starting with
  `k`..`z`, `0`, `I`, `O`, `l` have no prefix match (base58/varint limits).
* **Ports**: p2p/rpc/zmq = `40000 + (sha256(ticker) % 2000) * 10` + 0/1/2,
  bumped if another `chains/*.json` already has them.
* **Random, stored in the JSON** (re-running mkchain reuses them; `--fresh`
  rolls new ones): mainnet `NETWORK_ID` (testnet/stagenet ids derived from it),
  `GENESIS_NONCE`, genesis tx keys, premine tx key, hard-fork timestamp.
* **Consensus**: one hard-fork entry `{16, 1, 0, <creation time>}` so RandomX +
  the full v16 ruleset apply from block 1 (block 0 stays v1).

## Genesis and premine

`cnutil.py` builds the genesis coinbase directly (no C++ tool needed): a v1
coinbase at height 0 of exactly the v1 block reward (consensus requires
`reward == MONEY_SUPPLY >> EMISSION_SPEED_FACTOR`) to a random one-time key
whose secret is discarded (burned; Monero's genesis output is unspendable in
practice anyway), with tx pubkey + extra nonce `xmrfun:<TICKER>:<name>`.
mkchain also computes the genesis block hash the same way monerod does
(`keccak(varint(len) || header || tx_hash || varint(1))`); the self-test
reproduces Monero's `418015bb…` genesis hash, and `build.sh` fails if the built
daemon reports a different block 0 hash.

The premine can't live in genesis: wallets never scan block 0 and a v1 genesis
output can't be spent with v16 ring rules. Instead the chain gets a small
consensus patch (`blockchain.cpp`): if `PREMINE_TX` is set, block 1's coinbase
must be byte-for-byte `PREMINE_TX` (a v2 RingCT-null coinbase paying
`PREMINE_AMOUNT` to the premine address with a view tag, unlock height 61) and
`create_block_template` emits it for height 1. Whoever mines block 1, the coins
go to the premine address. Block 1 can therefore only be mined through
monerod's own block template (built-in miner or `get_block_template` without a
pool reserve that rewrites the coinbase). `--premine-address` accepts any
CryptoNote standard address (e.g. a Monero one); its keys are re-encoded with
the new chain's prefix, so the same seed restored in the chain's wallet sees
the coins.

## What gets stripped from Monero

* hard-coded mainnet checkpoints (`checkpoints.cpp`) and the compiled-in
  fast-sync block hashes `src/blocks/checkpoints.dat` (emptied; with Monero's
  hashes a node syncing the new chain from peers would reject its blocks once it
  has a full 512-block group);
* MoneroPulse DNS checkpoints, DNS blocklist and update checks (empty lists);
* Monero's DNS seed hostnames, hard-coded seed IPs (main/test/stagenet) and Tor/I2P
  seeds; only `--seed` values remain.

Left alone: `dns_utils.cpp` still probes `updates.moneropulse.org` once to
detect DNSSEC support (a DNS lookup, not a peer), and wallet-cli's `donate`
command points at Monero's address (fails to parse on the new chain).

## Seed node on Fly.io

The chain branch has `fly.toml` at the root plus `xmrfun/{Dockerfile,entrypoint.sh,chain.json}`.
The image builds monerod from the branch source (Debian bookworm) and runs a
full node with public P2P and `--restricted-rpc` (also on 443 via Fly TLS).
Steps are in the header of `fly.toml` (app, volume, dedicated IPv4 -- raw-TCP
P2P doesn't work on Fly's shared IPv4, then `fly deploy` from the worktree root
with submodules checked out). Nothing is deployed by the factory.

Chicken-and-egg: a seed's address has to be known before building. Allocate the
Fly IPv4 (or DNS name) first, then `mkchain.py --seed <ip> --force` and deploy.
