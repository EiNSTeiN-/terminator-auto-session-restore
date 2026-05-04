#!/usr/bin/env python3
"""Remove the Terminator auto session restore plugin for the current user."""

from __future__ import annotations

import os
from pathlib import Path

from configobj import ConfigObj


PLUGIN_NAME = "TerminatorAutoSessionRestore"


def disable_plugin(config_path: Path) -> None:
    if not config_path.exists():
        return

    config = ConfigObj(str(config_path), encoding="utf-8")
    enabled = config.get("global_config", {}).get("enabled_plugins", [])
    if isinstance(enabled, str):
        enabled = [part.strip() for part in enabled.split(",") if part.strip()]
    if PLUGIN_NAME in enabled:
        enabled.remove(PLUGIN_NAME)
        config["global_config"]["enabled_plugins"] = enabled

    layouts = config.get("layouts", {})
    layouts.pop(PLUGIN_NAME, None)
    config.write()


def remove(path: Path) -> None:
    try:
        path.unlink()
        print(f"Removed: {path}")
    except FileNotFoundError:
        pass


def main() -> None:
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))

    remove(config_home / "terminator" / "plugins" / "terminator_auto_session.py")
    remove(home / ".local" / "bin" / "terminator-auto-restore")
    remove(home / ".local" / "bin" / "terminator-pane-restore")
    remove(home / ".local" / "share" / "applications" / "terminator.desktop")
    remove(
        home
        / ".local"
        / "share"
        / "applications"
        / "terminator-auto-restore.desktop"
    )
    disable_plugin(config_home / "terminator" / "config")

    state_dir = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / (
        "terminator-auto-session"
    )
    print(f"Saved transcripts, if any, remain in: {state_dir}")


if __name__ == "__main__":
    main()
