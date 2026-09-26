// xmrfun: runtime chain identity. One monerod / wallet binary can run any xmrfun chain:
// `--chain-config <file.json>` rewrites the MAINNET parameters in cryptonote_config.h
// (config::chain::*, mainnet_config(), mainnet_hard_forks) before anything reads them.
// Schema: xmrfun-chains/README.md.
#pragma once

#include <string>
#include "common/command_line.h"

namespace cryptonote
{
  extern const command_line::arg_descriptor<std::string> arg_chain_config;

  bool load_chain_config(const std::string &path, std::string &error);

  // Looks for --chain-config in argv and loads it. Must run first thing in main(), before
  // option parsing. Returns false (after printing why) if the file is present but invalid.
  bool load_chain_config_from_argv(int argc, const char *const *argv);
}
