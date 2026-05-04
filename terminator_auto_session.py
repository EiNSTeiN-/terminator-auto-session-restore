"""Auto-save and restore Terminator sessions.

Install this file into ~/.config/terminator/plugins and enable the
TerminatorAutoSessionRestore plugin. It does not require changes to the
installed Terminator package.
"""

import json
import os
import re
import shlex
import signal
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GObject, Gtk, Vte

import terminatorlib.plugin as plugin
from terminatorlib.config import Config
from terminatorlib.terminator import Terminator
from terminatorlib.translation import _
from terminatorlib.util import dbg, err

try:
    import psutil
except ImportError:
    psutil = None


AVAILABLE = ["TerminatorAutoSessionRestore"]

LAYOUT_NAME = "TerminatorAutoSessionRestore"
SAVE_INTERVAL_SECONDS = 20
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "terminator-auto-session",
)
PANE_RESTORE_HELPER = os.path.expanduser("~/.local/bin/terminator-pane-restore")
UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
UUID_RE = re.compile(r"\b%s\b" % UUID_PATTERN)
CODEX_RESUME_RE = re.compile(
    r"(?:^|\s)(codex\s+resume(?:\s+--last)?(?:\s+%s)?)" % UUID_PATTERN
)
CLAUDE_RESUME_RE = re.compile(
    r"(?:^|\s)(claude\s+(?:--resume|-r)\s+%s|claude\s+(?:--continue|-c))"
    % UUID_PATTERN
)


class TerminatorAutoSessionRestore(plugin.MenuItem):
    """Save the current layout and Codex resume commands."""

    capabilities = ["terminal_menu", "session"]

    def __init__(self):
        plugin.MenuItem.__init__(self)
        self._term_handlers = {}
        self._vte_handlers = {}
        self._previous_signal_handlers = {}
        self._last_saved_signature = None
        self._last_save_at = 0
        self._timer_id = None
        self._connect_signals()
        self._install_signal_handlers()
        self._timer_id = GObject.timeout_add_seconds(
            SAVE_INTERVAL_SECONDS, self._periodic_save
        )

    def unload(self):
        for terminal, handler_id in list(self._term_handlers.items()):
            try:
                terminal.disconnect(handler_id)
            except TypeError:
                pass
        self._term_handlers.clear()

        for vte_terminal, handler_id in list(self._vte_handlers.items()):
            try:
                vte_terminal.disconnect(handler_id)
            except TypeError:
                pass
        self._vte_handlers.clear()

        if self._timer_id:
            GObject.source_remove(self._timer_id)
            self._timer_id = None

        for signum, previous in self._previous_signal_handlers.items():
            signal.signal(signum, previous)
        self._previous_signal_handlers.clear()

    def callback(self, menuitems, menu, terminal):
        save_item = Gtk.MenuItem.new_with_mnemonic(_("Save _Auto Session Now"))
        save_item.connect("activate", self._save_from_menu)
        menuitems.append(save_item)

        clear_item = Gtk.MenuItem.new_with_mnemonic(_("Clear Auto Session _Restore"))
        clear_item.connect("activate", self._clear_from_menu)
        menuitems.append(clear_item)

    def _connect_signals(self):
        for terminal in Terminator().terminals:
            if terminal not in self._term_handlers:
                self._term_handlers[terminal] = terminal.connect(
                    "pre-close-term", self._save_on_close
                )
            vte_terminal = terminal.get_vte()
            if vte_terminal not in self._vte_handlers:
                self._vte_handlers[vte_terminal] = vte_terminal.connect(
                    "child-exited", self._save_on_child_exit
                )

    def _install_signal_handlers(self):
        for signum in (signal.SIGHUP, signal.SIGTERM):
            previous = signal.getsignal(signum)
            if previous == self._handle_termination_signal:
                continue
            self._previous_signal_handlers[signum] = previous
            signal.signal(signum, self._handle_termination_signal)

    def _handle_termination_signal(self, signum, frame):
        self.save_session_layout(force=True)

        previous = self._previous_signal_handlers.get(signum, signal.SIG_DFL)
        if callable(previous):
            previous(signum, frame)
            return
        if previous == signal.SIG_IGN:
            return

        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _periodic_save(self):
        self._connect_signals()
        self.save_session_layout()
        return True

    def _save_on_close(self, _terminal, _event):
        self.save_session_layout(force=True)

    def _save_on_child_exit(self, _vte_terminal, _status):
        GObject.timeout_add(500, self._deferred_save_after_child_exit)

    def _deferred_save_after_child_exit(self):
        self.save_session_layout(force=True)
        return False

    def _save_from_menu(self, _menuitem):
        self.save_session_layout(force=True)

    def _clear_from_menu(self, _menuitem):
        config = Config()
        config.del_layout(LAYOUT_NAME)
        config.save()
        self._last_saved_signature = None

    def save_session_layout(self, force=False):
        terminator = Terminator()
        if terminator.doing_layout:
            return True

        now = time.monotonic()
        if not force and now - self._last_save_at < SAVE_INTERVAL_SECONDS:
            return True

        try:
            current_layout = terminator.describe_layout(save_cwd=True)
            self._add_restore_state(current_layout)
            signature = repr(current_layout)
            if not force and signature == self._last_saved_signature:
                return True

            config = Config()
            if not config.replace_layout(LAYOUT_NAME, current_layout):
                config.add_layout(LAYOUT_NAME, current_layout)
            config.save()
            self._last_saved_signature = signature
            self._last_save_at = now
            dbg("%s saved layout" % LAYOUT_NAME)
        except Exception as ex:
            err("%s failed to save layout: %s" % (LAYOUT_NAME, ex))
        return True

    def _add_restore_state(self, layout):
        terminals_by_uuid = {
            str(terminal.uuid): terminal for terminal in Terminator().terminals
        }

        for item in layout.values():
            if item.get("type") != "Terminal":
                continue
            terminal = terminals_by_uuid.get(str(item.get("uuid")))
            if terminal is None:
                continue

            pane_id = str(item.get("uuid"))
            restore_argv = self._find_resume_argv(terminal)
            self._write_pane_state(pane_id, terminal, restore_argv)
            item["command"] = "%s %s" % (
                shlex.quote(PANE_RESTORE_HELPER),
                shlex.quote(pane_id),
            )

    def _write_pane_state(self, pane_id, terminal, restore_argv):
        os.makedirs(STATE_DIR, exist_ok=True)
        transcript = self._capture_scrollback(terminal)
        transcript_path = os.path.join(STATE_DIR, "%s.txt" % pane_id)
        metadata_path = os.path.join(STATE_DIR, "%s.json" % pane_id)

        if transcript is not None:
            self._write_text_atomic(transcript_path, transcript)

        metadata = {
            "pane_id": pane_id,
            "cwd": terminal.get_cwd(),
            "saved_at": time.time(),
            "restore_argv": restore_argv,
            "transcript_path": transcript_path,
        }
        self._write_text_atomic(
            metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )

    def _write_text_atomic(self, path, content):
        tmp_path = "%s.tmp" % path
        with open(tmp_path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(content)
        os.replace(tmp_path, path)

    def _capture_scrollback(self, terminal):
        try:
            vte_terminal = terminal.get_vte()
            col, row = vte_terminal.get_cursor_position()
            row_start = 0
            row_end = max(row, 0)
            if Vte.get_minor_version() < 72:
                content = vte_terminal.get_text_range(
                    row_start, 0, row_end, col, lambda *args: True
                )[0]
            else:
                content = vte_terminal.get_text_range_format(
                    Vte.Format.TEXT, row_start, 0, row_end, col
                )[0]
            return content
        except Exception as ex:
            dbg("%s failed to capture scrollback: %s" % (LAYOUT_NAME, ex))
            return None

    def _find_resume_argv(self, terminal):
        return (
            self._find_resume_argv_in_scrollback(terminal)
            or self._find_resume_argv_from_process_tree(terminal)
        )

    def _find_resume_argv_in_scrollback(self, terminal):
        try:
            vte_terminal = terminal.get_vte()
            col, row = vte_terminal.get_cursor_position()
            row_start = max(0, row - 250)
            if Vte.get_minor_version() < 72:
                content = vte_terminal.get_text_range(
                    row_start, 0, row, col, lambda *args: True
                )[0]
            else:
                content = vte_terminal.get_text_range_format(
                    Vte.Format.TEXT, row_start, 0, row, col
                )[0]
        except Exception:
            return None

        for line in reversed(content.splitlines()):
            match = CODEX_RESUME_RE.search(line)
            if match:
                return shlex.split(match.group(1))
            match = CLAUDE_RESUME_RE.search(line)
            if match:
                return shlex.split(match.group(1))
        return None

    def _find_resume_argv_from_process_tree(self, terminal):
        if psutil is None or not terminal.pid:
            return None

        try:
            root = psutil.Process(terminal.pid)
            processes = [root] + root.children(recursive=True)
        except psutil.Error:
            return None

        fallback_argv = None
        for process in processes:
            process_kind = self._get_process_kind(process)
            if process_kind == "codex":
                fallback_argv = ["codex", "resume", "--last"]
                session_id = self._extract_session_id(process)
                if session_id:
                    return ["codex", "resume", session_id]
            elif process_kind == "claude":
                fallback_argv = ["claude", "--continue"]
                session_id = self._extract_session_id(process)
                if session_id:
                    return ["claude", "--resume", session_id]

        return fallback_argv

    def _get_process_kind(self, process):
        try:
            name = process.name() or ""
            cmdline = process.cmdline()
        except psutil.Error:
            return None

        values = [name] + cmdline
        basenames = [os.path.basename(value) for value in values if value]
        haystack = " ".join(values)
        if (
            "codex" in basenames
            or any(value.startswith("codex-") for value in basenames)
            or "@openai/codex" in haystack
        ):
            return "codex"
        if (
            "claude" in basenames
            or any(value.startswith("claude") for value in basenames)
            or "claude-code" in haystack
        ):
            return "claude"
        return None

    def _extract_session_id(self, process):
        try:
            environ = process.environ()
        except psutil.Error:
            environ = {}

        for key in (
            "CODEX_THREAD_ID",
            "CODEX_SESSION_ID",
            "CODEX_CONVERSATION_ID",
            "CLAUDE_SESSION_ID",
            "ANTHROPIC_SESSION_ID",
        ):
            value = environ.get(key)
            if value and UUID_RE.search(value):
                return UUID_RE.search(value).group(0)

        for value in environ.values():
            match = UUID_RE.search(value)
            if match and "TERMINATOR_UUID" not in value:
                return match.group(0)

        try:
            cmdline = process.cmdline()
        except psutil.Error:
            cmdline = []

        for value in cmdline:
            match = UUID_RE.search(value)
            if match:
                return match.group(0)
        return None
