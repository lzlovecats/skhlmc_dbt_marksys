#!/usr/bin/env python3
"""Outbound Workstation node service entry point."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from workstation.config import DEFAULT_CONFIG_PATH, load_config
from workstation.manager.ipc import DEFAULT_MANAGER_SOCKET, ManagerClient
from workstation.node.client import run_forever


def main() -> int:
    parser = argparse.ArgumentParser(description="LMC AI Workstation outbound node")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--manager-socket", type=Path, default=DEFAULT_MANAGER_SOCKET)
    args = parser.parse_args()
    asyncio.run(run_forever(load_config(args.config), ManagerClient(args.manager_socket)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
