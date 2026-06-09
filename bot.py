#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio

from pmpbot.config import Settings
from pmpbot.engine import BotEngine


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Polymarket 5-minute crypto probability bot")
    parser.add_argument("--once", action="store_true", help="run one evaluation cycle and exit")
    args = parser.parse_args()

    settings = Settings.load()
    print(f"settings_loaded mode={settings.execution_mode} assets={','.join(settings.assets)} env_path={Settings._load_env()}", flush=True)
    engine = BotEngine(settings)
    try:
        if args.once:
            await engine.run_once()
        else:
            await engine.run_forever()
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(amain())
