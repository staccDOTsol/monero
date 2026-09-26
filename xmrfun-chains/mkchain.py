#!/usr/bin/env python3
"""xmrfun chain factory: turn launch params into a new, independent RandomX
CryptoNote chain on branch chain/<TICKER> (in its own git worktree).

  python3 mkchain.py --name "Test Coin" --ticker TEST --block-time 60 \
      --premine-address 4... --premine 1000 --seed seed.example.com --seed 1.2.3.4

Re-running for an existing chains/<TICKER>.json reuses its random values
(network id, genesis keys, ...) so the result is reproducible; pass --fresh
to roll new ones.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time

import cnutil as cn

HERE = os.path.dirname(os.path.abspath(__file__))
CHAINS = os.path.join(HERE, "chains")
COIN = 10 ** 12
UINT64_MAX = 2 ** 64 - 1
FINAL_SUBSIDY_PER_MINUTE = 300000000000
# prefixes/ports already used by Monero (main/test/stage) and Cinderfork
TAKEN_PREFIXES = {18, 19, 42, 53, 54, 63, 24, 25, 36, 4242, 4243, 4244}
TAKEN_PORTS = {18080, 28080, 38080, 19080}


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------- derivation
def seed_int(ticker, tag):
    return int.from_bytes(hashlib.sha256(f"xmrfun:{ticker}:{tag}".encode()).digest(), "big")


def lead_chars(prefix):
    """Leading base58 chars every address with this prefix shares.

    An address is base58 over varint(prefix)||spend||view||checksum, encoded in
    8-byte blocks of 11 chars. The first block is varint(prefix) followed by
    random key bytes, so its leading chars are fixed wherever the smallest and
    largest possible block (key bytes all 00 vs all ff) agree.
    """
    v = cn.varint(prefix)
    lo = cn.b58encode(v + b"\x00" * (8 - len(v)))
    hi = cn.b58encode(v + b"\xff" * (8 - len(v)))
    n = 0
    while n < 11 and lo[n] == hi[n]:
        n += 1
    return lo[:n]


def pick_prefixes(ticker):
    """Three distinct address prefixes (standard, integrated, subaddress) whose
    addresses start with as much of the ticker as base58 allows (max 3 chars).

    Reachable first chars are '1'..'j': 1-byte varints (<128) give '1'..'N',
    2/3-byte varints (high bit set on the first byte) give 'N'..'j'.
    """
    want = ""
    for c in ticker:
        if c not in cn.B58 or len(want) == 3:
            break
        want += c
    def first_char_range(b0):  # first chars reachable when the address' first byte is b0
        lo = cn.b58encode(bytes([b0]) + b"\x00" * 7)[0]
        hi = cn.b58encode(bytes([b0]) + b"\xff" * 7)[0]
        return cn.B58.index(lo) <= cn.B58.index(want[0]) <= cn.B58.index(hi)

    candidates = [p for p in range(1, 128) if want and first_char_range(p)]
    for low7 in range(128):  # multi-byte varints (< 2^21): first byte is low7 | 0x80
        if want and first_char_range(low7 | 0x80):
            candidates += [low7 | (k << 7) for k in range(1, 1 << 14)]
    best = {}  # match length -> candidates
    for p in candidates:
        lc = lead_chars(p)
        if lc[:1] == want[:1] and p not in TAKEN_PREFIXES:
            best.setdefault(len(os.path.commonprefix([lc, want])), []).append(p)
    if not best:  # ticker starts with an unreachable char: fall back to anything
        best = {0: [p for p in range(128, 16384) if p not in TAKEN_PREFIXES]}
    pool = []
    for n in sorted(best, reverse=True):
        pool += sorted(best[n], key=lambda p: hashlib.sha256(f"{ticker}:{p}".encode()).digest())
        if len(pool) >= 3:
            break
    return pool[:3]


def pick_ports(ticker, used):
    base = 40000 + (seed_int(ticker, "ports") % 2000) * 10  # 40000..59990, step 10
    while any(p in used or p in TAKEN_PORTS for p in (base, base + 1, base + 2)):
        base = 40000 + (base - 40000 + 10) % 20000
    return {"p2p": base, "rpc": base + 1, "zmq": base + 2}


def used_ports(ticker):
    used = set()
    for f in os.listdir(CHAINS) if os.path.isdir(CHAINS) else []:
        if f.endswith(".json") and f != f"{ticker}.json":
            with open(os.path.join(CHAINS, f)) as fh:
                used |= set(json.load(fh)["ports"].values())
    return used


def base_reward(supply, generated, esf, target):
    minutes = target // 60
    r = (supply - generated) >> (esf - (minutes - 1))
    return max(r, FINAL_SUBSIDY_PER_MINUTE * minutes)


def parse_seeds(seeds, p2p_port):
    dns, ips = [], []
    for s in seeds:
        host, _, port = s.rpartition(":") if s.count(":") == 1 else (s, "", "")
        port = int(port or p2p_port)
        try:
            ipaddress.IPv4Address(host)
            ips.append(f"{host}:{port}")
        except ValueError:
            if port != p2p_port:
                sys.exit(f"seed {s}: DNS seeds must use the chain's p2p port {p2p_port}")
            dns.append(host)
    return dns, ips


def derive(a, prev):
    t = a.ticker
    supply = int(a.supply * COIN) if a.supply else UINT64_MAX
    if supply > UINT64_MAX:
        sys.exit(f"--supply max is {UINT64_MAX / COIN:.0f} coins (uint64 atomic units, 12 decimals)")
    if a.block_time % 60 or a.block_time < 60:
        sys.exit("--block-time must be a positive multiple of 60 (consensus static_assert)")
    if not 1 <= a.emission_speed - (a.block_time // 60 - 1) <= 63:
        sys.exit("--emission-speed out of range for this block time")

    std, integ, sub = pick_prefixes(t)
    ports = pick_ports(t, used_ports(t))
    dns_seeds, ip_seeds = parse_seeds(a.seed, ports["p2p"])

    keep = prev if prev and not a.fresh else {}
    rnd = keep.get("random") or {
        "network_id": os.urandom(16).hex(),
        "genesis_nonce": int.from_bytes(os.urandom(4), "little"),
        "genesis_tx_secret": hex(cn.random_scalar()),
        "genesis_burn_secret": hex(cn.random_scalar()),
        "premine_tx_secret": hex(cn.random_scalar()),
        "hardfork_time": int(time.time()),
    }

    # Genesis: v1 coinbase for exactly the v1 block reward (consensus requires it),
    # paid to a throwaway key whose secret is never stored, i.e. burned.
    genesis_amount = base_reward(supply, 0, a.emission_speed, 60)
    nonce_text = f"xmrfun:{t}:{a.name}".encode()[:127]
    gtx = cn.genesis_tx(genesis_amount, cn.pubkey(int(rnd["genesis_tx_secret"], 16)),
                        cn.pubkey(int(rnd["genesis_burn_secret"], 16)), nonce_text)
    genesis = {
        "tx_hex": gtx.hex(),
        "nonce": rnd["genesis_nonce"],
        "amount_atomic": genesis_amount,
        "tx_hash": cn.tx_hash(gtx).hex(),
        "block_hash": cn.genesis_block_hash(gtx.hex(), rnd["genesis_nonce"]),
        "extra_nonce_text": nonce_text.decode(errors="replace"),
    }

    premine = None
    if a.premine_address and a.premine:
        _, spend, view = cn.decode_address(a.premine_address)
        amount = int(a.premine * COIN)
        if amount >= supply - genesis_amount:
            sys.exit("--premine must be below total supply")
        r = int(rnd["premine_tx_secret"], 16)
        blob, plen, out_key = cn.coinbase_v2(1, amount, spend, view, r)
        premine = {
            "address": cn.encode_address(std, spend, view),
            "given_address": a.premine_address,
            "amount_atomic": amount,
            "height": 1,
            "tx_hex": blob.hex(),
            "tx_hash": cn.tx_hash(blob, plen).hex(),
            "tx_secret_key": r.to_bytes(32, "little").hex(),
            "output_key": out_key.hex(),
        }

    nid = bytes.fromhex(rnd["network_id"])
    return {
        "name": a.name,
        "ticker": t,
        "branch": f"chain/{t}",
        "cryptonote_name": "xmrfun-" + re.sub(r"[^a-z0-9]", "", t.lower()),
        "money_supply_atomic": supply,
        "emission_speed_factor_per_minute": a.emission_speed,
        "difficulty_target_v2": a.block_time,
        "block1_reward_atomic": premine["amount_atomic"] if premine else
            base_reward(supply, genesis_amount, a.emission_speed, a.block_time),
        "prefixes": {
            "address": std, "integrated": integ, "subaddress": sub,
            "address_starts_with": lead_chars(std),
            "integrated_starts_with": lead_chars(integ),
            "subaddress_starts_with": lead_chars(sub),
        },
        "ports": ports,
        "network_ids": {
            "mainnet": nid.hex(),
            "testnet": bytes(nid[:15] + bytes([nid[15] ^ 1])).hex(),
            "stagenet": bytes(nid[:15] + bytes([nid[15] ^ 2])).hex(),
        },
        "hardforks": [{"version": 16, "height": 1, "threshold": 0, "time": rnd["hardfork_time"]}],
        "genesis": genesis,
        "premine": premine,
        "seed_nodes": {"dns": dns_seeds, "ip": ip_seeds},
        "random": rnd,
    }


# ---------------------------------------------------------------- patching
def sub1(text, pattern, repl, count=1, flags=0, what=None):
    new, n = re.subn(pattern, repl, text, count=count, flags=flags)
    if n == 0:
        sys.exit(f"patch anchor not found: {what or pattern}")
    return new


def uuid_init(hexid):
    b = bytes.fromhex(hexid)
    return "{ {\n      " + ", ".join(f"0x{x:02X}" for x in b) + "\n    } }"


def patch_config(s, c):
    g, pm = c["genesis"], c["premine"]
    s = sub1(s, r'(#define CRYPTONOTE_NAME\s+)"[^"]*"', rf'\1"{c["cryptonote_name"]}"')
    s = sub1(s, r"(#define MONEY_SUPPLY\s+).*", rf"\1((uint64_t){c['money_supply_atomic']}ull)")
    s = sub1(s, r"(#define EMISSION_SPEED_FACTOR_PER_MINUTE\s+).*", rf"\1({c['emission_speed_factor_per_minute']})")
    s = sub1(s, r"(#define DIFFICULTY_TARGET_V2\s+)\d+", rf"\g<1>{c['difficulty_target_v2']}")
    # mainnet values come first in namespace config; count=1 leaves testnet/stagenet alone
    for key, val in [("CRYPTONOTE_PUBLIC_ADDRESS_BASE58_PREFIX", c["prefixes"]["address"]),
                     ("CRYPTONOTE_PUBLIC_INTEGRATED_ADDRESS_BASE58_PREFIX", c["prefixes"]["integrated"]),
                     ("CRYPTONOTE_PUBLIC_SUBADDRESS_BASE58_PREFIX", c["prefixes"]["subaddress"]),
                     ("P2P_DEFAULT_PORT", c["ports"]["p2p"]),
                     ("RPC_DEFAULT_PORT", c["ports"]["rpc"]),
                     ("ZMQ_RPC_DEFAULT_PORT", c["ports"]["zmq"])]:
        s = sub1(s, rf"({key} = )\d+", rf"\g<1>{val}")
    ids = iter([c["network_ids"]["mainnet"], c["network_ids"]["testnet"], c["network_ids"]["stagenet"]])
    s = sub1(s, r"(NETWORK_ID = )\{ \{.*?\} \};[^\n]*",
             lambda m: m.group(1) + uuid_init(next(ids)) + ";", count=3, flags=re.S)
    s = sub1(s, r'(GENESIS_TX = )"[0-9a-f]*";', rf'\1"{g["tx_hex"]}";')
    s = sub1(s, r"(uint32_t const GENESIS_NONCE = )\d+;",
             rf"\g<1>{g['nonce']};\n"
             f"  // xmrfun premine: block 1's coinbase must be exactly this tx (empty = no premine)\n"
             f"  std::string const PREMINE_TX = \"{pm['tx_hex'] if pm else ''}\";\n"
             f"  uint64_t const PREMINE_AMOUNT = {pm['amount_atomic'] if pm else 0};")
    return s


PREMINE_VALIDATE = r"""
  // xmrfun premine: block 1's coinbase is fixed at chain creation (config::PREMINE_TX)
  if (!::config::PREMINE_TX.empty() && boost::get<txin_gen>(b.miner_tx.vin[0]).height == 1)
  {
    cryptonote::blobdata premine_blob;
    CHECK_AND_ASSERT_MES(epee::string_tools::parse_hexstr_to_binbuff(::config::PREMINE_TX, premine_blob), false, "bad PREMINE_TX");
    CHECK_AND_ASSERT_MES(tx_to_blob(b.miner_tx) == premine_blob, false, "block 1 must carry the premine coinbase");
    CHECK_AND_ASSERT_MES(fee == 0, false, "block 1 must not contain transactions");
    base_reward = ::config::PREMINE_AMOUNT;
    partial_block_reward = false;
    return true;
  }
"""

PREMINE_TEMPLATE = r"""
  // xmrfun premine: block 1 carries only the fixed premine coinbase
  if (!::config::PREMINE_TX.empty() && height == 1)
  {
    cryptonote::blobdata premine_blob;
    CHECK_AND_ASSERT_MES(epee::string_tools::parse_hexstr_to_binbuff(::config::PREMINE_TX, premine_blob)
        && parse_and_validate_tx_from_blob(premine_blob, b.miner_tx), false, "bad PREMINE_TX");
    b.tx_hashes.clear();
    expected_reward = ::config::PREMINE_AMOUNT;
    cumulative_weight = get_transaction_weight(b.miner_tx);
    if (!from_block)
      cache_block_template(b, miner_address, ex_nonce, diffic, height, expected_reward, cumulative_weight, seed_height, seed_hash, pool_cookie, include_sensitive);
    return true;
  }
"""


def patch_blockchain(s, c):
    s = sub1(s, r"(bool Blockchain::validate_miner_transaction\(.*?\n\{\n  LOG_PRINT_L3\(\"Blockchain::\" << __func__\);\n)",
             lambda m: m.group(1) + PREMINE_VALIDATE, flags=re.S, what="validate_miner_transaction")
    s = sub1(s, r"(  pool_cookie = m_tx_pool\.cookie\(\);\n)", lambda m: m.group(1) + PREMINE_TEMPLATE,
             what="create_block_template pool_cookie")
    return s


def patch_hardforks(s, c):
    hf = c["hardforks"][0]
    s = sub1(s, r"const hardfork_t mainnet_hard_forks\[\] = \{.*?\n\};",
             "const hardfork_t mainnet_hard_forks[] = {\n"
             "  // v1 for the genesis block only. Without this entry HardFork has no v1 placeholder\n"
             "  // (it only adds one for an empty table), reports v16 on an empty DB and rejects genesis.\n"
             "  { 1, 0, 0, 1341378000 },\n"
             f"  // xmrfun {c['ticker']}: full v16 ruleset (RandomX PoW) from block 1\n"
             f"  {{ {hf['version']}, {hf['height']}, {hf['threshold']}, {hf['time']} }},\n}};", flags=re.S)
    return sub1(s, r"(mainnet_hard_fork_version_1_till = )\d+;", r"\g<1>0;")


def patch_checkpoints(s, c):
    # drop the stock Monero mainnet checkpoints (whatever is between the stagenet block and the end)
    s = sub1(s, r"(if \(nettype == STAGENET\)\n    \{.*?return true;\n    \}\n).*?(    return true;\n  \}\n\n  bool checkpoints::load_checkpoints_from_json)",
             lambda m: m.group(1) + f"    // xmrfun {c['ticker']}: independent chain, no Monero checkpoints\n" + m.group(2),
             flags=re.S, what="init_default_checkpoints")
    return empty_string_vectors(s, ["dns_urls", "testnet_dns_urls", "stagenet_dns_urls"])


def empty_string_vectors(s, names):
    for n in names:
        s = sub1(s, rf"(static const std::vector<std::string> {n} = )\{{.*?\}};", r"\1{}; // xmrfun: no MoneroPulse DNS",
                 flags=re.S, what=n)
    return s


def cpp_list(items, indent):
    return "".join(f"{indent}full_addrs.insert(\"{x}\");\n" for x in items)


def patch_net_node_inl(s, c):
    body = ("  std::set<std::string> node_server<t_payload_net_handler>::get_ip_seed_nodes() const\n  {\n"
            "    // xmrfun: only this chain's own seeds; never Monero's\n"
            "    std::set<std::string> full_addrs;\n"
            "    if (m_nettype == cryptonote::MAINNET)\n    {\n"
            + cpp_list(c["seed_nodes"]["ip"], "      ") +
            "    }\n    return full_addrs;\n  }\n")
    s = sub1(s, r"  std::set<std::string> node_server<t_payload_net_handler>::get_ip_seed_nodes\(\) const\n  \{\n.*?\n  \}\n",
             lambda m: body, flags=re.S, what="get_ip_seed_nodes")
    # Monero's Tor / I2P seed lists
    s = sub1(s, r"(case epee::net_utils::zone::tor:\n).*?(    case epee::net_utils::zone::i2p:\n).*?(    default:)",
             lambda m: m.group(1) + "      return {};\n" + m.group(2) + "      return {};\n" + m.group(3),
             flags=re.S, what="tor/i2p seeds")
    s = sub1(s, r"(static const std::vector<std::string> dns_urls = )\{.*?\};", r"\1{}; // xmrfun: no Monero DNS blocklist",
             flags=re.S, what="blocklist dns_urls")
    return s


def patch_net_node_h(s, c):
    items = "".join(f"\n    {',' if i else ' '} \"{h}\"" for i, h in enumerate(c["seed_nodes"]["dns"]))
    return sub1(s, r"const std::vector<std::string> m_seed_nodes_list =\n    \{.*?\};",
                "const std::vector<std::string> m_seed_nodes_list =\n    {" + items + "\n    };",
                flags=re.S, what="m_seed_nodes_list")


PATCHES = [
    ("src/cryptonote_config.h", patch_config),
    ("src/hardforks/hardforks.cpp", patch_hardforks),
    ("src/checkpoints/checkpoints.cpp", patch_checkpoints),
    ("src/cryptonote_core/blockchain.cpp", patch_blockchain),
    ("src/p2p/net_node.inl", patch_net_node_inl),
    ("src/p2p/net_node.h", patch_net_node_h),
    ("src/common/updates.cpp", lambda s, c: empty_string_vectors(s, ["dns_urls"])),
]


def render(src, dst, c):
    with open(src) as fh:
        text = fh.read()
    for k, v in {"TICKER": c["ticker"], "ticker": c["ticker"].lower(), "NAME": c["name"],
                 "CRYPTONOTE_NAME": c["cryptonote_name"], "P2P_PORT": c["ports"]["p2p"],
                 "RPC_PORT": c["ports"]["rpc"]}.items():
        text = text.replace("{{" + k + "}}", str(v))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w") as fh:
        fh.write(text)


def apply_chain(c, wt):
    for rel, fn in PATCHES:
        path = os.path.join(wt, rel)
        with open(path) as fh:
            s = fh.read()
        with open(path, "w") as fh:
            fh.write(fn(s, c))
    # compiled-in Monero block hashes (fast sync) would reject this chain's blocks past height ~512
    open(os.path.join(wt, "src/blocks/checkpoints.dat"), "w").close()
    for name in ("Dockerfile", "entrypoint.sh"):
        render(os.path.join(HERE, "deploy", name), os.path.join(wt, "xmrfun", name), c)
    render(os.path.join(HERE, "deploy", "fly.toml"), os.path.join(wt, "fly.toml"), c)
    render(os.path.join(HERE, "deploy", "dockerignore"), os.path.join(wt, ".dockerignore"), c)
    os.chmod(os.path.join(wt, "xmrfun", "entrypoint.sh"), 0o755)
    with open(os.path.join(wt, "xmrfun", "chain.json"), "w") as fh:
        json.dump({k: v for k, v in c.items() if k != "random"}, fh, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--ticker", required=True, type=lambda s: s.upper())
    ap.add_argument("--supply", type=float, help="total supply in coins (default: Monero's 2^64-1 atomic)")
    ap.add_argument("--emission-speed", type=int, default=20, help="EMISSION_SPEED_FACTOR_PER_MINUTE (Monero: 20; lower = faster)")
    ap.add_argument("--block-time", type=int, default=120, help="DIFFICULTY_TARGET_V2 seconds, multiple of 60")
    ap.add_argument("--premine-address", help="any CryptoNote standard address; its keys are re-encoded for this chain")
    ap.add_argument("--premine", type=float, help="premine in coins, paid by a fixed block-1 coinbase")
    ap.add_argument("--seed", action="append", default=[], help="seed node: IPv4[:port] or DNS hostname (repeatable)")
    ap.add_argument("--base", default="HEAD", help="git ref to fork from (default: HEAD)")
    ap.add_argument("--worktree", help="worktree path (default: xmrfun-chains/work/<TICKER>)")
    ap.add_argument("--fresh", action="store_true", help="roll new random values even if chains/<TICKER>.json exists")
    ap.add_argument("--force", action="store_true", help="replace an existing chain/<TICKER> branch + worktree")
    a = ap.parse_args()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", a.ticker):
        sys.exit("ticker must be 2-10 chars A-Z/0-9")
    if bool(a.premine_address) != bool(a.premine):
        sys.exit("--premine and --premine-address go together")

    os.makedirs(CHAINS, exist_ok=True)
    json_path = os.path.join(CHAINS, f"{a.ticker}.json")
    prev = json.load(open(json_path)) if os.path.exists(json_path) else None
    c = derive(a, prev)

    repo = sh("git", "rev-parse", "--show-toplevel", cwd=HERE)
    wt = os.path.abspath(a.worktree or os.path.join(HERE, "work", a.ticker))
    base = sh("git", "rev-parse", a.base, cwd=repo)
    if a.force and os.path.isdir(wt):
        # reset the existing worktree in place; untracked build/ survives for incremental rebuilds
        sh("git", "checkout", "-q", "-f", "-B", c["branch"], base, cwd=wt)
    else:
        sh("git", "worktree", "add", "-b", c["branch"], wt, base, cwd=repo)
    apply_chain(c, wt)
    sh("git", "add", "-A", cwd=wt)
    sh("git", "-c", "user.name=xmrfun", "-c", "user.email=chains@xmrfun.invalid", "commit", "-q",
       "-m", f"{c['name']} ({a.ticker}): new chain from xmrfun factory", cwd=wt)

    c.update(base_commit=base, commit=sh("git", "rev-parse", "HEAD", cwd=wt), worktree=wt,
             created_at=prev["created_at"] if prev and not a.fresh else int(time.time()))
    with open(json_path, "w") as fh:
        json.dump(c, fh, indent=2)
        fh.write("\n")

    p = c["prefixes"]
    print(f"{c['name']} ({a.ticker}) -> branch {c['branch']} @ {c['commit'][:10]}, worktree {wt}")
    print(f"  addresses start with '{p['address_starts_with']}' (prefix {p['address']}), "
          f"integrated '{p['integrated_starts_with']}' ({p['integrated']}), subaddress '{p['subaddress_starts_with']}' ({p['subaddress']})")
    print(f"  ports p2p/rpc/zmq {c['ports']['p2p']}/{c['ports']['rpc']}/{c['ports']['zmq']}, network id {c['network_ids']['mainnet']}")
    print(f"  genesis block {c['genesis']['block_hash']}")
    if c["premine"]:
        print(f"  premine {c['premine']['amount_atomic'] / COIN} {a.ticker} in block 1 -> {c['premine']['address']}")
    print(f"  wrote {json_path}")


if __name__ == "__main__":
    main()
