#!/usr/bin/env python3
"""AI Workstation Manager service entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from workstation.config import DEFAULT_CONFIG_PATH, load_config
from workstation.manager.arbiter import ModeArbiter
from workstation.manager.ipc import DEFAULT_MANAGER_SOCKET, ManagerApplication, serve_manager
from workstation.manager.state_store import StateStore


def main() -> int:
    parser = argparse.ArgumentParser(description="LMC AI Workstation Manager")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--socket", type=Path, default=DEFAULT_MANAGER_SOCKET)
    args = parser.parse_args()
    config = load_config(args.config)
    arbiter = ModeArbiter(StateStore(config.paths.state / "manager-state.json"))
    serve_manager(ManagerApplication(config, arbiter), args.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
