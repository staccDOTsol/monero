# xmrfun chains: one binary, one JSON per coin

Every xmrfun coin is its own RandomX CryptoNote chain, but they all run the same
`monerod` / `monero-wallet-rpc` / `monero-wallet-cli` build. A chain is a JSON file
passed as `--chain-config <file>`. It is read first thing in `main()` and rewrites the
mainnet parameters before anything else looks at them. Launching a coin means
generating a JSON and deploying the prebuilt image with it. Nothing gets recompiled.

Without `--chain-config` the binaries are Cinderfork, as before (prefix 4242, ports
19080/19081, `~/.cinderfork`).

```
python3 chainconfig.py --name "Staccx" --ticker STACCX -o staccx.json   # new chain
monerod --chain-config staccx.json                                      # node
monero-wallet-rpc --chain-config staccx.json --daemon-address 127.0.0.1:<rpc_port> ...
python3 verify.py                                                       # local end-to-end test
```

| file | what |
|---|---|
| `chainconfig.py` | launch params -> chain JSON (random network id, genesis tx + nonce, optional premine); `--check FILE` prints a config's genesis hash |
| `cnutil.py` | stdlib keccak / ed25519 / base58 / tx serialization used to build genesis + premine txs (`python3 cnutil.py` self-tests against Monero's genesis hash) |
| `Dockerfile` | the single image: monerod + monero-wallet-rpc + `xmrfun-chainconfig` |
| `entrypoint.sh` | `CHAIN_CONFIG_JSON` env (raw or base64) -> `/data/chain.json` -> monerod, public P2P 18080 + restricted RPC 18081 |
| `fly.chain.toml` | one Fly app per coin |
| `verify.py` | two chains from one binary, mining, peer refusal, wallet-rpc, no-flag Cinderfork check |

## Chain config schema

```jsonc
{
  "cryptonote_name": "xmrfun-staccx",   // required: data dir (~/.xmrfun-staccx) and default log/conf names
  "network_id": "0d6491d8...c21a",       // required: 32 hex chars (16 bytes); peers with another id are refused
  "genesis_tx": "013c01ff00...",         // required: hex coinbase tx of block 0
  "genesis_nonce": 3175513742,           // required: uint32
  "launch_time": 1790387000,             // unix time; required only if hard_forks is omitted
  "hard_forks": [{"version": 16, "height": 1, "threshold": 0, "time": 1790387000}],
                                         // default: [{16, 1, 0, launch_time}], i.e. RandomX + v16 rules from block 1.
                                         // {1, 0, 0, ...} is always prepended (genesis is a v1 block).
  "p2p_port": 52830, "rpc_port": 52831, "zmq_port": 52832,   // default ports (defaults: Cinderfork's)
  "money_supply": "18446744073709551615",   // MONEY_SUPPLY, atomic units (12 decimals); number or decimal string
  "emission_speed_factor": 20,              // EMISSION_SPEED_FACTOR_PER_MINUTE
  "final_subsidy_per_minute": "300000000000", // tail emission, atomic units per minute
  "difficulty_target": 120,                 // DIFFICULTY_TARGET_V2 (block time), multiple of 60, 60..3600
  "address_prefixes": {"standard": 18, "integrated": 19, "subaddress": 42},  // default: stock Monero
  "seed_nodes": ["203.0.113.10:18080"],     // IPv4:port only; DNS seeds are off for config chains
  "premine": {"tx": "02...", "amount": "1000000000000000"},  // optional, see below
  "name": "Staccx Coin", "ticker": "STACCX", "genesis_hash": "5d59...",   // informational, ignored by the loader
}
```

`uint64` fields can be JSON numbers or decimal strings (JavaScript can't represent
2^64-1). The loader validates the whole file, including that `genesis_tx` parses,
before it changes anything. On any error the binary prints why and exits 1.

## What `--chain-config` changes (MAINNET only)

* `CRYPTONOTE_NAME`, `NETWORK_ID`, `GENESIS_TX`, `GENESIS_NONCE`, default P2P/RPC/ZMQ
  ports, address prefixes (`cryptonote_config.h`: `config::chain::*` globals plus a
  mutable `mainnet_config()` behind `get_config()`).
* `MONEY_SUPPLY`, `EMISSION_SPEED_FACTOR_PER_MINUTE`, `FINAL_SUBSIDY_PER_MINUTE`,
  `DIFFICULTY_TARGET_V2`. These macros now expand to the globals, so every use site
  reads the runtime value. The one `static_assert` and one `static constexpr` that used
  them were changed; the loader enforces the multiple-of-60 rule instead.
* The hard-fork table: `mainnet_hard_forks` is now a pointer + count.
* Command-line defaults that were computed during static initialization (`--data-dir`,
  `--config-file`, `--log-file`, `--p2p-bind-port`, `--p2p-bind-port-ipv6`,
  `--rpc-bind-port`, `--zmq-rpc-bind-port`) are recomputed when a chain config is
  active and the flag was left at its default. `--help` still prints the Cinderfork
  defaults.
* Turned off for config chains: MoneroPulse DNS checkpoints, the DNS blocklist, update
  checks, DNS seeds, Monero's hard-coded seed IPs and Tor/I2P seeds, and the
  compiled-in Monero fast-sync hashes (`src/blocks/checkpoints.dat`). With those hashes a
  node syncing a new chain from peers would reject its blocks once it had a full
  512-block group. Only `seed_nodes` remain.

`--chain-config` must be given on the command line, not in a `--config-file`, because
it is read before option parsing.

## Addresses and key reuse

Config chains keep Monero's address prefixes by default, so a stock wallet (monero-ts,
the browser wallet) works on every coin, and a STACCX address looks like a Monero
address (`4...`). As a result the same keys/seed give the same address on Monero and
on every xmrfun chain. Reusing them is the user's call. Two consequences to document
for users:

* Privacy: spending the same outputs' keys on two chains can link transactions across
  chains (key images are chain-independent).
* Nothing in an address says which chain it belongs to. The wallet's `--chain-config`
  (or the daemon it talks to) decides.

## Genesis and premine

`chainconfig.py` builds the genesis coinbase directly. It is a v1 coinbase at height 0
of exactly the v1 block reward, which consensus requires:
`max(MONEY_SUPPLY >> EMISSION_SPEED_FACTOR, FINAL_SUBSIDY)`. It pays a random one-time
key whose secret is discarded, i.e. burned. Its extra field carries a tx pubkey and the
nonce `xmrfun:<TICKER>:<name>`. The random keys, `genesis_nonce` and `network_id` make
every launch unique, even for the same ticker. The genesis block hash is computed the
same way monerod does. `verify.py` checks the daemon agrees, and the self-test
reproduces Monero's `418015bb…`.

The premine can't live in genesis: wallets never scan block 0, and a v1 genesis output
can't be spent under v16 ring rules. Instead, if `premine` is set, block 1's coinbase
must be byte-for-byte `premine.tx`. That is a v2 RingCT-null coinbase paying the amount
to the premine address, with a view tag and unlock height 61, and
`create_block_template` emits it at height 1. Whoever mines block 1, the coins go to the
premine address. Block 1 has to come from monerod's own template (built-in miner, or
`get_block_template` used as-is, without a pool rewriting the coinbase).

## Fly.io

Build the image once, then each coin is a new app running that image with its own
`CHAIN_CONFIG_JSON` secret and a volume. The exact commands are in the headers of
`Dockerfile` and `fly.chain.toml`. P2P is raw TCP on 18080 and needs a dedicated IPv4
(`fly ips allocate-v4`). The restricted RPC is served on 18081 (http) and 443 (https).
`get_block_template` / `submit_block` are allowed on restricted RPC, so the stratum can
use it. The node itself doesn't mine. `seed_nodes` aren't consensus, so they can be
added to the JSON after the first node's IP is known without changing the chain.
