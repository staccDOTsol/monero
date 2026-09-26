"""python3 -m xmrfun_stratum --config chains.json"""

import argparse
import asyncio
import logging
import os

from .pool import Pool


def main():
    ap = argparse.ArgumentParser(description="xmrfun profit-switching multi-chain RandomX stratum")
    ap.add_argument("--config", default=os.environ.get("XMRFUN_CONFIG", "chains.json"))
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    pool = Pool(args.config)
    try:
        asyncio.run(pool.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
