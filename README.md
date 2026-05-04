# Terminator Auto Session Restore

Terminator Auto Session Restore is a user-level plugin for the
[Terminator](https://github.com/gnome-terminator/terminator) terminal emulator.
It restores Terminator windows, tabs, split panes, working directories, and
saved terminal transcripts after a logout or reboot. For tools that have their
own resume support, it can also start the appropriate resume command in the
restored pane.

The plugin does not require patching Terminator or replacing the Ubuntu
`terminator` package.

## What It Restores

- Terminator windows, tabs, and split panes.
- Pane working directories.
- The visible VTE scrollback transcript for each pane.
- A fresh login shell after the transcript is replayed.
- Codex sessions when a `codex resume ...` command or Codex session ID is
  detectable.
- Claude Code sessions when a `claude --resume ...`, `claude -r ...`, or
  `claude --continue` restore target is detectable.

## What It Cannot Restore

This is not a generic process checkpoint system.

After a normal reboot, Linux destroys each terminal's PTY, process memory, file
descriptors, foreground jobs, and in-memory shell state. A terminal emulator
cannot resume an arbitrary process such as `vim`, `ssh`, `python`, `npm run
dev`, or an interactive shell exactly where it was unless that program has its
own resume mechanism or it was running inside another persistence layer.

For generic programs, this plugin restores the transcript and then starts a new
shell. That means you see the previous commands and output in the pane, but the
original process is not still alive.

For exact continuation of arbitrary interactive programs, use one of these
approaches instead or in addition:

- Hibernate instead of rebooting.
- Run work inside `tmux`, `zellij`, or `screen` and restore that multiplexer.
- Use application-specific resume support, such as `codex resume` or
  `claude --resume`.
- Use CRIU-style checkpoint/restore for workloads that explicitly support it.

## Fresh Ubuntu Install

These steps assume Ubuntu with GNOME and the stock Ubuntu `terminator` package.

```bash
sudo apt update
sudo apt install -y terminator git python3-configobj python3-psutil
git clone https://github.com/EiNSTeiN-/terminator-auto-session-restore.git
cd terminator-auto-session-restore
python3 install.py
```

Then close every running Terminator window and launch Terminator again from the
GNOME app grid, dock, or this command:

```bash
~/.local/bin/terminator-auto-restore
```

The installer creates a user desktop override at:

```text
~/.local/share/applications/terminator.desktop
```

That override makes normal desktop launches call `terminator-auto-restore`.

## How It Works

Terminator already has a plugin system and a layout format. This project uses
both.

`terminator_auto_session.py` is installed into:

```text
~/.config/terminator/plugins/terminator_auto_session.py
```

The installer enables the plugin by adding `TerminatorAutoSessionRestore` to
`enabled_plugins` in:

```text
~/.config/terminator/config
```

While Terminator is running, the plugin saves a layout named
`TerminatorAutoSessionRestore` every 20 seconds and again when panes or the
Terminator process exit. For each pane it writes metadata and transcript files
under:

```text
~/.local/state/terminator-auto-session/
```

When Terminator is launched with no command-line arguments and there is no
existing Terminator process, `terminator-auto-restore` starts:

```bash
/usr/bin/terminator -l TerminatorAutoSessionRestore
```

Each restored pane runs:

```bash
~/.local/bin/terminator-pane-restore <pane-id>
```

That helper prints the saved transcript, runs a detected app-specific resume
command when one exists, and finally starts a fresh login shell.

## Shell History

The transcript restore is independent of shell history. It shows previous
commands and command output in the terminal pane even if your shell did not
flush history before reboot.

For normal shell history files, configure your shell to append history
frequently.

For Bash, add or keep settings like these in `~/.bashrc`:

```bash
shopt -s histappend
HISTSIZE=100000
HISTFILESIZE=200000
PROMPT_COMMAND="history -a; history -n${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
```

For Zsh, use settings like these in `~/.zshrc`:

```zsh
HISTSIZE=100000
SAVEHIST=200000
setopt APPEND_HISTORY
setopt INC_APPEND_HISTORY
setopt SHARE_HISTORY
```

## Privacy

Saved transcripts are plaintext. If your terminal shows secrets, tokens,
passwords, private source code, or production data, those bytes may be written
under:

```text
~/.local/state/terminator-auto-session/
```

Clear saved restore data with:

```bash
rm -rf ~/.local/state/terminator-auto-session
```

You can also remove the saved restore layout from the Terminator context menu:
right-click a terminal and choose `Clear Auto Session Restore`.

## Manual Commands

Install or update the plugin:

```bash
python3 install.py
```

Launch without auto-restore:

```bash
TERMINATOR_NO_AUTO_RESTORE=1 ~/.local/bin/terminator-auto-restore
```

Force the saved layout:

```bash
terminator -l TerminatorAutoSessionRestore
```

Uninstall the user-level plugin and launcher:

```bash
python3 uninstall.py
```

The uninstall script leaves saved transcripts in place and prints their path so
you can decide whether to delete them.

## Troubleshooting

Check that the plugin is enabled:

```bash
grep -n "TerminatorAutoSessionRestore" ~/.config/terminator/config
```

Check that the helper scripts are installed:

```bash
ls -l ~/.local/bin/terminator-auto-restore ~/.local/bin/terminator-pane-restore
```

Check for saved state:

```bash
ls -l ~/.local/state/terminator-auto-session
```

Run Terminator with debug logs:

```bash
TERMINATOR_NO_AUTO_RESTORE=1 terminator --debug
```

If the GNOME app grid still launches `/usr/bin/terminator` directly, refresh the
desktop database or log out and back in:

```bash
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

## Development

The project intentionally avoids modifying Terminator source code. Keep changes
limited to the user plugin, launcher, helper scripts, and documentation unless
there is a specific reason to require an upstream Terminator patch.

Basic local checks:

```bash
python3 -m py_compile install.py uninstall.py terminator_auto_session.py terminator-pane-restore
sh -n terminator-auto-restore
```

## License

GPL-2.0-only. See `LICENSE`.
