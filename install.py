#!/usr/bin/env python3
"""Install the Terminator auto session restore plugin for the current user."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from configobj import ConfigObj


PLUGIN_NAME = "TerminatorAutoSessionRestore"
DEFAULT_PLUGINS = [
    "LaunchpadBugURLHandler",
    "LaunchpadCodeURLHandler",
    "APTURLHandler",
]


def install_file(src: Path, dest: Path, mode: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(mode)


def ensure_plugin_enabled(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(
            '[global_config]\n[keybindings]\n[profiles]\n'
            '  [[default]]\n[layouts]\n'
            '  [[default]]\n'
            '    [[[window0]]]\n'
            '      type = Window\n'
            '      parent = ""\n'
            '    [[[child1]]]\n'
            '      type = Terminal\n'
            '      parent = window0\n'
            '[plugins]\n',
            encoding="utf-8",
        )

    config = ConfigObj(str(config_path), encoding="utf-8")
    config.setdefault("global_config", {})
    enabled = config["global_config"].get("enabled_plugins", DEFAULT_PLUGINS[:])
    if isinstance(enabled, str):
        enabled = [part.strip() for part in enabled.split(",") if part.strip()]
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
    config["global_config"]["enabled_plugins"] = enabled
    config.indent_type = "  "
    config.write()


def write_desktop_file(dest: Path, wrapper: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Name=Terminator",
                "Comment=Multiple terminals in one window",
                "TryExec=/usr/bin/terminator",
                f"Exec={wrapper}",
                "Icon=terminator",
                "Type=Application",
                "Categories=GNOME;GTK;Utility;TerminalEmulator;System;",
                "StartupNotify=true",
                "X-Ubuntu-Gettext-Domain=terminator",
                "X-Ayatana-Desktop-Shortcuts=NewWindow;",
                "Keywords=terminal;shell;prompt;command;commandline;",
                "",
                "[NewWindow Shortcut Group]",
                "Name=Open a New Window",
                f"Exec={wrapper}",
                "TargetEnvironment=Unity",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> None:
    source_dir = Path(__file__).resolve().parent
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))

    plugin_dest = config_home / "terminator" / "plugins" / "terminator_auto_session.py"
    wrapper_dest = home / ".local" / "bin" / "terminator-auto-restore"
    pane_restore_dest = home / ".local" / "bin" / "terminator-pane-restore"
    desktop_dest = home / ".local" / "share" / "applications" / "terminator.desktop"
    config_path = config_home / "terminator" / "config"

    install_file(source_dir / "terminator_auto_session.py", plugin_dest, 0o644)
    install_file(source_dir / "terminator-auto-restore", wrapper_dest, 0o755)
    install_file(source_dir / "terminator-pane-restore", pane_restore_dest, 0o755)
    ensure_plugin_enabled(config_path)
    write_desktop_file(desktop_dest, wrapper_dest)

    print(f"Installed plugin: {plugin_dest}")
    print(f"Installed launcher: {wrapper_dest}")
    print(f"Installed pane restore helper: {pane_restore_dest}")
    print(f"Installed desktop override: {desktop_dest}")
    print(f"Enabled plugin: {PLUGIN_NAME}")


if __name__ == "__main__":
    main()
