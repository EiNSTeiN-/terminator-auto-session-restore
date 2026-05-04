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
- Codex sessions when a `codex resume <session-id>` line or Codex session ID is
  detectable from the running Codex process or local Codex session files.
- Claude Code sessions when a `claude --resume <session-id>`,
  `claude -r <session-id>`, or Claude session ID is detectable from the running
  Claude process or local Claude project files.

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
It also creates a separate GNOME app-grid launcher:

```text
~/.local/share/applications/terminator-auto-restore.desktop
```

Search for `Terminator Auto Restore` in GNOME Shell if you want a distinct
launcher that can be pinned separately from the stock Terminator entry.

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

That helper prints the saved transcript, draws a separator showing when the
pane was saved, runs a detected app-specific resume command when one exists,
and finally starts a fresh login shell. On VTE versions that support styled
HTML capture, new saves also replay foreground/background colors by converting
the saved HTML transcript back to ANSI color sequences.

Saved layouts include window size and position. When saving, off-screen window
geometry is adjusted to the current monitor work area so a restored Terminator
window does not come back outside the visible viewport.

Codex and Claude restore does not depend on those tools exiting cleanly and
printing their final resume hints. When they are still running during a
Terminator close, the plugin tries to derive the exact session ID from their
process environment and local session files. The restore helper validates exact
Codex and Claude session IDs against local session files before running them.

## Crash Recovery

Normal Terminator closes capture the latest styled transcript before the pane
exits. For crash recovery, the plugin also checkpoints lightweight state while
Terminator is running:

- Layout, pane working directories, and Codex/Claude process-derived resume
  commands are refreshed periodically.
- Plaintext transcript checkpoints are captured periodically for the full
  available terminal scrollback, so the plugin does not intentionally truncate
  saved transcript state.
- Styled HTML transcript capture is reserved for clean close, child exit,
  termination signal, or manual `Save Auto Session Now`, because HTML capture is
  the expensive path most likely to make typing feel sluggish.

After a hard power loss, the latest checkpoint may be behind by one interval
and the operating system may still lose very recent filesystem writes. The
design is therefore best-effort crash recovery, not a transactional terminal
recorder.

## Shell History

The transcript restore is independent of shell history. It shows previous
commands and command output in the terminal pane even if your shell did not
flush history before reboot.

For restored Bash panes, `terminator-pane-restore` assigns a pane-specific
history file under:

```text
~/.local/state/terminator-auto-session/shell-history/
```

It starts Bash with a generated rcfile that sources your normal `~/.bashrc`,
then forces `HISTFILE` to the pane-specific file and appends/refreshes history
on each prompt. That keeps arrow-up history separate per restored pane.

For non-restored shells, or shells other than Bash, configure your shell to
append history frequently.

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

Saved transcripts and pane-specific Bash history files are plaintext, with
styled HTML transcript copies on VTE versions that support color capture. If
your terminal shows secrets, tokens, passwords, private source code, or
production data, those bytes may be written under:

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

The installer also writes a user-level desktop override at:

```text
~/.local/share/applications/terminator.desktop
```

and a separate app-grid launcher at:

```text
~/.local/share/applications/terminator-auto-restore.desktop
```

That desktop entry launches `~/.local/bin/terminator-auto-restore`, so the
GNOME dock and app grid can use auto-restore without you typing the wrapper
command manually. The separate entry appears as `Terminator Auto Restore` and
can be pinned independently. If the dock still launches the old system
Terminator entry after install, fully close Terminator and log out/in, or unpin
and re-pin Terminator so GNOME refreshes its launcher cache.

Launch without auto-restore:

```bash
TERMINATOR_NO_AUTO_RESTORE=1 ~/.local/bin/terminator-auto-restore
```

Force the saved layout in a separate no-DBus Terminator process, even while
another Terminator process is already running:

```bash
TERMINATOR_FORCE_AUTO_RESTORE=1 ~/.local/bin/terminator-auto-restore
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
