// xmrfun: see chain_config.h
#include "chain_config.h"

#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <vector>
#include "rapidjson/document.h"
#include "string_tools.h"
#include "cryptonote_config.h"
#include "cryptonote_basic/cryptonote_format_utils.h"
#include "hardforks/hardforks.h"

namespace cryptonote
{
  const command_line::arg_descriptor<std::string> arg_chain_config = {
    "chain-config", "JSON file with this chain's identity (xmrfun; applies to mainnet)", ""};

  namespace
  {
    // Monero mainnet block 0 coinbase (used with nonce 10000)
    const char STOCK_GENESIS_TX[] = "013c01ff0001ffffffffffff03029b2e4c0281c0b02e7c53291a94d1d0cbff8883f8024f5142ee494ffbbd08807121017767aafcde9be00dcfd098715ebcf7f410daebc582fda69d24a28e9d0bc890d1";

    // uint64 values may be JSON numbers or decimal strings (JS can't hold 2^64-1 as a number)
    bool get_u64(const rapidjson::Value &d, const char *key, uint64_t &out, bool required, std::string &err)
    {
      if (!d.HasMember(key))
      {
        if (required)
          err = std::string("missing \"") + key + "\"";
        return !required;
      }
      const rapidjson::Value &v = d[key];
      if (v.IsUint64())
        out = v.GetUint64();
      else if (v.IsString() && v.GetStringLength() > 0 && std::strspn(v.GetString(), "0123456789") == v.GetStringLength())
        out = std::stoull(v.GetString());
      else
      {
        err = std::string("\"") + key + "\" must be an unsigned integer";
        return false;
      }
      return true;
    }

    bool get_str(const rapidjson::Value &d, const char *key, std::string &out, std::string &err)
    {
      if (!d.HasMember(key) || !d[key].IsString())
      {
        err = std::string("missing string \"") + key + "\"";
        return false;
      }
      out = d[key].GetString();
      return true;
    }
  }

  bool load_chain_config(const std::string &path, std::string &err)
  {
    std::ifstream f(path);
    if (!f)
    {
      err = "cannot open " + path;
      return false;
    }
    std::stringstream ss;
    ss << f.rdbuf();
    rapidjson::Document d;
    if (d.Parse(ss.str().c_str()).HasParseError() || !d.IsObject())
    {
      err = "not a JSON object";
      return false;
    }

    config_t &c = mainnet_config();
    // Genesis defaults to Monero's own block 0 so stock wallets (which hard-code it) can sync;
    // chains are told apart by network_id and everything from block 1 on.
    std::string name, network_id, genesis_tx = STOCK_GENESIS_TX;
    uint64_t nonce = 10000, launch_time = 0;
    if (!get_str(d, "cryptonote_name", name, err) || !get_str(d, "network_id", network_id, err)
        || (d.HasMember("genesis_tx") && !get_str(d, "genesis_tx", genesis_tx, err))
        || !get_u64(d, "genesis_nonce", nonce, false, err))
      return false;
    if (name.empty() || name.find_first_of("/\\") != std::string::npos)
      return err = "bad cryptonote_name", false;
    if (network_id.size() != 32 || !epee::string_tools::hex_to_pod(network_id, c.NETWORK_ID))
      return err = "network_id must be 32 hex chars", false;
    blobdata blob;
    transaction genesis;
    if (!epee::string_tools::parse_hexstr_to_binbuff(genesis_tx, blob) || !parse_and_validate_tx_from_blob(blob, genesis))
      return err = "genesis_tx does not parse as a transaction", false;
    if (nonce > 0xffffffff)
      return err = "genesis_nonce must fit in 32 bits", false;

    // Stock Monero address prefixes by default so standard wallets/libraries work on every chain.
    uint64_t prefix = 18, integrated = 19, subaddress = 42;
    if (d.HasMember("address_prefixes"))
    {
      const rapidjson::Value &p = d["address_prefixes"];
      if (!p.IsObject() || !get_u64(p, "standard", prefix, true, err) || !get_u64(p, "integrated", integrated, true, err)
          || !get_u64(p, "subaddress", subaddress, true, err))
        return err = "address_prefixes: " + err, false;
    }

    uint64_t p2p = c.P2P_DEFAULT_PORT, rpc = c.RPC_DEFAULT_PORT, zmq = c.ZMQ_RPC_DEFAULT_PORT;
    uint64_t supply = config::chain::money_supply, speed = config::chain::emission_speed_factor_per_minute;
    uint64_t tail = config::chain::final_subsidy_per_minute, target = config::chain::difficulty_target_v2;
    if (!get_u64(d, "p2p_port", p2p, false, err) || !get_u64(d, "rpc_port", rpc, false, err) || !get_u64(d, "zmq_port", zmq, false, err)
        || !get_u64(d, "money_supply", supply, false, err) || !get_u64(d, "emission_speed_factor", speed, false, err)
        || !get_u64(d, "final_subsidy_per_minute", tail, false, err) || !get_u64(d, "difficulty_target", target, false, err)
        || !get_u64(d, "launch_time", launch_time, false, err))
      return false;
    if (p2p > 65535 || rpc > 65535 || zmq > 65535)
      return err = "ports must be <= 65535", false;
    if (target == 0 || target % 60 || target > 3600)
      return err = "difficulty_target must be a multiple of 60 seconds (60..3600)", false;
    if (speed < target / 60 || speed - (target / 60 - 1) > 63)
      return err = "emission_speed_factor out of range for this difficulty_target", false;
    if (supply <= get_outs_money_amount(genesis))
      return err = "money_supply must exceed the genesis output", false;

    // Hard forks: default is RandomX + v16 rules from block 1. A v1 entry at height 0 is
    // always put first, since genesis is a v1 block (see hardforks.cpp).
    static std::vector<hardfork_t> forks;
    forks.clear();
    forks.emplace_back(1, 0, 0, 1341378000);
    if (d.HasMember("hard_forks"))
    {
      if (!d["hard_forks"].IsArray())
        return err = "hard_forks must be an array", false;
      for (const auto &h : d["hard_forks"].GetArray())
      {
        uint64_t version, height, threshold = 0, time;
        if (!h.IsObject() || !get_u64(h, "version", version, true, err) || !get_u64(h, "height", height, true, err)
            || !get_u64(h, "threshold", threshold, false, err) || !get_u64(h, "time", time, true, err))
          return err = "hard_forks: " + err, false;
        if (version == 1 && height == 0)
          continue;
        const hardfork_t &last = forks.back();
        if (version > 255 || threshold > 100 || version <= last.version || height <= last.height || (time_t)time <= last.time)
          return err = "hard_forks must strictly increase in version, height and time", false;
        forks.emplace_back(version, height, threshold, time);
      }
    }
    else
    {
      if (launch_time <= 1341378000)
        return err = "launch_time (unix seconds) is required when hard_forks is not given", false;
      forks.emplace_back(16, 1, 0, launch_time);
    }

    std::vector<std::string> seeds;
    if (d.HasMember("seed_nodes"))
    {
      if (!d["seed_nodes"].IsArray())
        return err = "seed_nodes must be an array of \"ip:port\"", false;
      for (const auto &s : d["seed_nodes"].GetArray())
      {
        if (!s.IsString())
          return err = "seed_nodes must be an array of \"ip:port\"", false;
        seeds.push_back(s.GetString());
      }
    }

    std::string premine_tx;
    uint64_t premine_amount = 0;
    if (d.HasMember("premine"))
    {
      const rapidjson::Value &p = d["premine"];
      transaction t;
      if (!p.IsObject() || !get_str(p, "tx", premine_tx, err) || !get_u64(p, "amount", premine_amount, true, err))
        return err = "premine: " + err, false;
      if (!epee::string_tools::parse_hexstr_to_binbuff(premine_tx, blob) || !parse_and_validate_tx_from_blob(blob, t))
        return err = "premine.tx does not parse as a transaction", false;
    }

    // everything validated: apply
    config::chain::name() = name;
    c.GENESIS_TX = genesis_tx;
    c.GENESIS_NONCE = nonce;
    c.CRYPTONOTE_PUBLIC_ADDRESS_BASE58_PREFIX = prefix;
    c.CRYPTONOTE_PUBLIC_INTEGRATED_ADDRESS_BASE58_PREFIX = integrated;
    c.CRYPTONOTE_PUBLIC_SUBADDRESS_BASE58_PREFIX = subaddress;
    c.P2P_DEFAULT_PORT = p2p;
    c.RPC_DEFAULT_PORT = rpc;
    c.ZMQ_RPC_DEFAULT_PORT = zmq;
    config::chain::money_supply = supply;
    config::chain::emission_speed_factor_per_minute = speed;
    config::chain::final_subsidy_per_minute = tail;
    config::chain::difficulty_target_v2 = target;
    config::chain::seed_nodes() = seeds;
    config::chain::premine_tx() = premine_tx;
    config::chain::premine_amount = premine_amount;
    mainnet_hard_forks = forks.data();
    num_mainnet_hard_forks = forks.size();
    mainnet_hard_fork_version_1_till = 0;
    config::chain::active = true;
    return true;
  }

  bool load_chain_config_from_argv(int argc, const char *const *argv)
  {
    static const std::string flag = "--" + std::string(arg_chain_config.name);
    std::string path;
    for (int i = 1; i < argc; ++i)
    {
      if (argv[i] == flag && i + 1 < argc)
        path = argv[i + 1];
      else if (std::strncmp(argv[i], (flag + "=").c_str(), flag.size() + 1) == 0)
        path = argv[i] + flag.size() + 1;
    }
    if (path.empty())
      return true;
    std::string err;
    if (!load_chain_config(path, err))
    {
      std::cerr << "Error: --chain-config " << path << ": " << err << std::endl;
      return false;
    }
    return true;
  }
}
