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
import uuid

import gi
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GObject, Gtk, Vte

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
TRANSCRIPT_CHECKPOINT_INTERVAL_SECONDS = 60
CONNECT_INTERVAL_SECONDS = 2
LAYOUT_CHANGE_SAVE_DELAY_MS = 1000
WINDOW_CLOSE_SUPPRESS_SECONDS = 2
MIN_RESTORED_WINDOW_WIDTH = 100
MIN_RESTORED_WINDOW_HEIGHT = 80
CAPTURE_NONE = "none"
CAPTURE_TEXT = "text"
CAPTURE_FULL = "full"
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
RESTORE_SEPARATOR_MARKERS = (
    "previous shell ended",
    "restored transcript ends",
)
CODEX_RESUME_RE = re.compile(r"\bcodex\s+resume\s+(?P<session_id>%s)\b" % UUID_PATTERN)
CLAUDE_RESUME_RE = re.compile(
    r"\bclaude\s+(?:--resume|-r)\s+(?P<session_id>%s)\b" % UUID_PATTERN
)
CODEX_SESSION_ENV_KEYS = (
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_CONVERSATION_ID",
)
CLAUDE_SESSION_ENV_KEYS = (
    "CLAUDE_SESSION_ID",
    "ANTHROPIC_SESSION_ID",
)
CODEX_SESSION_PATH_MARKERS = (
    "/.codex/shell_snapshots/",
    "/.codex/sessions/",
)
RECENT_SESSION_SCAN_LIMIT = 80


class TerminatorAutoSessionRestore(plugin.MenuItem):
    """Save the current layout and Codex resume commands."""

    capabilities = ["terminal_menu", "session"]

    def __init__(self):
        plugin.MenuItem.__init__(self)
        self._term_handlers = {}
        self._vte_handlers = {}
        self._window_handlers = {}
        self._previous_signal_handlers = {}
        self._cwd_by_pane = {}
        self._last_saved_signature = None
        self._last_save_at = 0
        self._last_transcript_checkpoint_at = 0
        self._layout_change_save_id = None
        self._ignore_degraded_close_saves_until = 0
        self._window_close_snapshot_terminal_count = 0
        self._timer_id = None
        self._terminal_class = None
        self._original_terminal_reconfigure = None
        self._install_terminal_hooks()
        self._connect_signals()
        self._install_signal_handlers()
        self._timer_id = GObject.timeout_add_seconds(
            CONNECT_INTERVAL_SECONDS, self._periodic_save
        )

    def unload(self):
        for terminal, handlers in list(self._term_handlers.items()):
            for handler_id in handlers.values():
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

        for window, handlers in list(self._window_handlers.items()):
            for handler_id in handlers.values():
                try:
                    window.disconnect(handler_id)
                except TypeError:
                    pass
        self._window_handlers.clear()

        if self._timer_id:
            GObject.source_remove(self._timer_id)
            self._timer_id = None

        if self._layout_change_save_id:
            GObject.source_remove(self._layout_change_save_id)
            self._layout_change_save_id = None

        for signum, previous in self._previous_signal_handlers.items():
            signal.signal(signum, previous)
        self._previous_signal_handlers.clear()

        self._uninstall_terminal_hooks()

    def callback(self, menuitems, menu, terminal):
        save_item = Gtk.MenuItem.new_with_mnemonic(_("Save _Auto Session Now"))
        save_item.connect("activate", self._save_from_menu)
        menuitems.append(save_item)

        clear_item = Gtk.MenuItem.new_with_mnemonic(_("Clear Auto Session _Restore"))
        clear_item.connect("activate", self._clear_from_menu)
        menuitems.append(clear_item)

    def _connect_signals(self):
        for terminal in Terminator().terminals:
            self._connect_terminal(terminal)
        for window in Terminator().windows:
            self._connect_window(window)

    def _connect_terminal(self, terminal):
        handlers = self._term_handlers.setdefault(terminal, {})
        if "pre-close-term" not in handlers:
            handlers["pre-close-term"] = terminal.connect(
                "pre-close-term", self._save_on_pre_close
            )
        if "close-term" not in handlers:
            handlers["close-term"] = terminal.connect(
                "close-term", self._save_on_close_term
            )
        for signal_name in (
            "split-auto",
            "split-horiz",
            "split-vert",
            "tab-new",
            "move-tab",
            "rotate-cw",
            "rotate-ccw",
        ):
            if signal_name not in handlers:
                handlers[signal_name] = terminal.connect(
                    signal_name, self._queue_layout_change_save
                )

        try:
            vte_terminal = terminal.get_vte()
        except Exception:
            return
        if vte_terminal not in self._vte_handlers:
            self._vte_handlers[vte_terminal] = vte_terminal.connect(
                "child-exited", self._save_on_child_exit
            )

    def _connect_window(self, window):
        handlers = self._window_handlers.setdefault(window, {})
        for signal_name in ("configure-event", "window-state-event"):
            if signal_name not in handlers:
                handlers[signal_name] = window.connect(
                    signal_name, self._queue_layout_change_save
                )

    def _install_terminal_hooks(self):
        try:
            from terminatorlib.terminal import Terminal
        except Exception as ex:
            dbg("%s failed to install terminal hooks: %s" % (LAYOUT_NAME, ex))
            return

        current_reconfigure = Terminal.reconfigure
        if getattr(current_reconfigure, "_auto_session_restore_wrapped", False):
            return

        plugin_self = self

        def reconfigure_wrapper(terminal, *args, **kwargs):
            plugin_self._connect_terminal(terminal)
            return current_reconfigure(terminal, *args, **kwargs)

        reconfigure_wrapper._auto_session_restore_wrapped = True
        reconfigure_wrapper._auto_session_restore_plugin = self
        self._terminal_class = Terminal
        self._original_terminal_reconfigure = current_reconfigure
        Terminal.reconfigure = reconfigure_wrapper

    def _uninstall_terminal_hooks(self):
        if not self._terminal_class or not self._original_terminal_reconfigure:
            return

        current_reconfigure = self._terminal_class.reconfigure
        if getattr(current_reconfigure, "_auto_session_restore_plugin", None) is self:
            self._terminal_class.reconfigure = self._original_terminal_reconfigure
        self._terminal_class = None
        self._original_terminal_reconfigure = None

    def _install_signal_handlers(self):
        for signum in (signal.SIGHUP, signal.SIGTERM):
            previous = signal.getsignal(signum)
            if previous == self._handle_termination_signal:
                continue
            self._previous_signal_handlers[signum] = previous
            signal.signal(signum, self._handle_termination_signal)

    def _handle_termination_signal(self, signum, frame):
        self.save_session_layout(
            force=True, capture_mode=CAPTURE_FULL, include_scrollback_resume=True
        )

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
        now = time.monotonic()
        capture_mode = CAPTURE_NONE
        if now - self._last_transcript_checkpoint_at >= TRANSCRIPT_CHECKPOINT_INTERVAL_SECONDS:
            capture_mode = CAPTURE_TEXT
        self.save_session_layout(
            capture_mode=capture_mode, include_scrollback_resume=False
        )
        return True

    def _save_on_pre_close(self, _terminal, *_args):
        terminal_count = self._terminal_count()
        if self._should_skip_degraded_close_save(terminal_count):
            return

        self.save_session_layout(
            force=True, capture_mode=CAPTURE_FULL, include_scrollback_resume=True
        )
        if terminal_count > 1:
            self._window_close_snapshot_terminal_count = max(
                self._window_close_snapshot_terminal_count, terminal_count
            )
            self._ignore_degraded_close_saves_until = (
                time.monotonic() + WINDOW_CLOSE_SUPPRESS_SECONDS
            )

    def _save_on_close_term(self, _terminal, *_args):
        if self._should_skip_degraded_close_save():
            return
        self.save_session_layout(
            force=True, capture_mode=CAPTURE_FULL, include_scrollback_resume=True
        )

    def _save_on_child_exit(self, _vte_terminal, _status):
        if self._should_skip_degraded_close_save():
            return
        self.save_session_layout(
            force=True, capture_mode=CAPTURE_FULL, include_scrollback_resume=True
        )

    def _terminal_count(self):
        return len(Terminator().terminals)

    def _should_skip_degraded_close_save(self, terminal_count=None):
        now = time.monotonic()
        if now > self._ignore_degraded_close_saves_until:
            self._window_close_snapshot_terminal_count = 0
            self._ignore_degraded_close_saves_until = 0
            return False

        if terminal_count is None:
            terminal_count = self._terminal_count()
        return terminal_count < self._window_close_snapshot_terminal_count

    def _queue_layout_change_save(self, *_args):
        if self._layout_change_save_id is None:
            self._layout_change_save_id = GObject.timeout_add(
                LAYOUT_CHANGE_SAVE_DELAY_MS, self._deferred_layout_change_save
            )

    def _deferred_layout_change_save(self):
        self._layout_change_save_id = None
        self._connect_signals()
        self.save_session_layout(
            force=True, capture_mode=CAPTURE_NONE, include_scrollback_resume=False
        )
        return False

    def _save_from_menu(self, _menuitem):
        self.save_session_layout(
            force=True, capture_mode=CAPTURE_FULL, include_scrollback_resume=True
        )

    def _clear_from_menu(self, _menuitem):
        config = Config()
        config.del_layout(LAYOUT_NAME)
        config.save()
        self._last_saved_signature = None

    def save_session_layout(
        self, force=False, capture_mode=CAPTURE_NONE, include_scrollback_resume=False
    ):
        terminator = Terminator()
        if terminator.doing_layout:
            return True

        now = time.monotonic()
        if (
            not force
            and capture_mode == CAPTURE_NONE
            and now - self._last_save_at < SAVE_INTERVAL_SECONDS
        ):
            return True

        try:
            current_layout = self._normalise_layout_for_config(
                terminator.describe_layout(save_cwd=False)
            )
            self._add_restore_state(
                current_layout, capture_mode, include_scrollback_resume
            )
            self._apply_window_geometry_safety(current_layout)
            current_layout = self._normalise_layout_for_config(current_layout)
            signature = repr(current_layout)
            if not force and signature == self._last_saved_signature:
                self._last_save_at = now
                if capture_mode != CAPTURE_NONE:
                    self._last_transcript_checkpoint_at = now
                return True

            config = Config()
            if not config.replace_layout(LAYOUT_NAME, current_layout):
                config.add_layout(LAYOUT_NAME, current_layout)
            config.save()
            self._last_saved_signature = signature
            self._last_save_at = now
            if capture_mode != CAPTURE_NONE:
                self._last_transcript_checkpoint_at = now
            dbg("%s saved layout" % LAYOUT_NAME)
        except Exception as ex:
            err("%s failed to save layout: %s" % (LAYOUT_NAME, ex))
        return True

    def _add_restore_state(self, layout, capture_mode, include_scrollback_resume):
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
            restore_argv = self._find_resume_argv(terminal, include_scrollback_resume)
            cwd = self._write_pane_state(
                pane_id, terminal, restore_argv, capture_mode
            )
            item["directory"] = cwd
            item["command"] = "%s %s" % (
                shlex.quote(PANE_RESTORE_HELPER),
                shlex.quote(pane_id),
            )

    def _normalise_layout_for_config(self, value):
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, dict):
            return {
                str(key): self._normalise_layout_for_config(child)
                for key, child in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._normalise_layout_for_config(child) for child in value]
        return value

    def _apply_window_geometry_safety(self, layout):
        workareas = self._screen_workareas()
        if not workareas:
            return

        for item in layout.values():
            if item.get("type") != "Window":
                continue

            position = self._parse_window_position(item.get("position"))
            if position is None:
                continue

            size = self._parse_window_size(item.get("size")) or (1, 1)
            adjusted = self._fit_window_rect_to_workarea(position, size, workareas)
            if adjusted is None:
                continue

            new_position, new_size = adjusted
            item["position"] = "%d:%d" % new_position
            if self._parse_window_size(item.get("size")) is not None:
                item["size"] = [int(new_size[0]), int(new_size[1])]

    def _screen_workareas(self):
        try:
            display = Gdk.Display.get_default()
            if display is None:
                return []

            workareas = []
            get_n_monitors = getattr(display, "get_n_monitors", None)
            if callable(get_n_monitors):
                for index in range(display.get_n_monitors()):
                    monitor = display.get_monitor(index)
                    if monitor is None:
                        continue
                    rect = monitor.get_workarea()
                    workareas.append((rect.x, rect.y, rect.width, rect.height))
                return [area for area in workareas if area[2] > 1 and area[3] > 1]

            screen = Gdk.Screen.get_default()
            if screen is None:
                return []
            for index in range(screen.get_n_monitors()):
                rect = screen.get_monitor_workarea(index)
                workareas.append((rect.x, rect.y, rect.width, rect.height))
            return [area for area in workareas if area[2] > 1 and area[3] > 1]
        except Exception as ex:
            dbg("%s failed to inspect screen workareas: %s" % (LAYOUT_NAME, ex))
            return []

    def _parse_window_position(self, value):
        if isinstance(value, str):
            parts = value.split(":", 1)
        elif isinstance(value, (list, tuple)):
            parts = value
        else:
            return None

        if len(parts) < 2:
            return None
        try:
            return int(float(parts[0])), int(float(parts[1]))
        except (TypeError, ValueError):
            return None

    def _parse_window_size(self, value):
        if isinstance(value, str):
            parts = re.split(r"[:,x]", value, maxsplit=1)
        elif isinstance(value, (list, tuple)):
            parts = value
        else:
            return None

        if len(parts) < 2:
            return None
        try:
            width = int(float(parts[0]))
            height = int(float(parts[1]))
        except (TypeError, ValueError):
            return None
        if width <= 1 or height <= 1:
            return None
        return width, height

    def _fit_window_rect_to_workarea(self, position, size, workareas):
        x, y = position
        width, height = size

        for area in workareas:
            if self._rect_fits_area(x, y, width, height, area):
                return position, size

        area = self._best_workarea_for_rect(x, y, width, height, workareas)
        if area is None:
            return None

        area_x, area_y, area_width, area_height = area
        fitted_width = min(width, area_width)
        fitted_height = min(height, area_height)
        if width >= MIN_RESTORED_WINDOW_WIDTH:
            fitted_width = max(min(MIN_RESTORED_WINDOW_WIDTH, area_width), fitted_width)
        if height >= MIN_RESTORED_WINDOW_HEIGHT:
            fitted_height = max(
                min(MIN_RESTORED_WINDOW_HEIGHT, area_height), fitted_height
            )

        max_x = area_x + max(area_width - fitted_width, 0)
        max_y = area_y + max(area_height - fitted_height, 0)
        fitted_x = min(max(x, area_x), max_x)
        fitted_y = min(max(y, area_y), max_y)

        return (int(fitted_x), int(fitted_y)), (int(fitted_width), int(fitted_height))

    def _rect_fits_area(self, x, y, width, height, area):
        area_x, area_y, area_width, area_height = area
        return (
            x >= area_x
            and y >= area_y
            and x + width <= area_x + area_width
            and y + height <= area_y + area_height
        )

    def _best_workarea_for_rect(self, x, y, width, height, workareas):
        scored_areas = []
        rect_center_x = x + width / 2.0
        rect_center_y = y + height / 2.0

        for area in workareas:
            area_x, area_y, area_width, area_height = area
            overlap_width = max(
                0, min(x + width, area_x + area_width) - max(x, area_x)
            )
            overlap_height = max(
                0, min(y + height, area_y + area_height) - max(y, area_y)
            )
            overlap = overlap_width * overlap_height
            area_center_x = area_x + area_width / 2.0
            area_center_y = area_y + area_height / 2.0
            distance = (
                (rect_center_x - area_center_x) ** 2
                + (rect_center_y - area_center_y) ** 2
            )
            scored_areas.append((overlap, -distance, area))

        if not scored_areas:
            return None
        return max(scored_areas)[2]

    def _write_pane_state(self, pane_id, terminal, restore_argv, capture_mode):
        os.makedirs(STATE_DIR, exist_ok=True)
        transcript_path = os.path.join(STATE_DIR, "%s.txt" % pane_id)
        html_transcript_path = os.path.join(STATE_DIR, "%s.html" % pane_id)
        metadata_path = os.path.join(STATE_DIR, "%s.json" % pane_id)

        html_transcript = None
        if capture_mode != CAPTURE_NONE:
            transcript, html_transcript = self._capture_scrollback(terminal, capture_mode)
            if transcript is not None:
                self._write_text_atomic(transcript_path, transcript)
            if html_transcript is not None:
                self._write_text_atomic(html_transcript_path, html_transcript)

        cwd = self._get_terminal_cwd(pane_id, terminal)
        metadata = {
            "pane_id": pane_id,
            "cwd": cwd,
            "saved_at": time.time(),
            "restore_argv": restore_argv,
            "transcript_path": transcript_path,
        }
        if self._should_use_html_transcript(
            html_transcript_path, transcript_path, capture_mode, html_transcript
        ):
            metadata["html_transcript_path"] = html_transcript_path
        self._write_text_atomic(
            metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )
        return cwd

    def _write_text_atomic(self, path, content):
        tmp_path = "%s.tmp" % path
        with open(tmp_path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(content)
        os.replace(tmp_path, path)

    def _should_use_html_transcript(
        self, html_transcript_path, transcript_path, capture_mode, html_transcript
    ):
        if html_transcript is not None:
            return True
        if capture_mode != CAPTURE_NONE:
            return False
        try:
            return os.path.getmtime(html_transcript_path) >= os.path.getmtime(
                transcript_path
            )
        except OSError:
            return False

    def _capture_scrollback(self, terminal, capture_mode):
        try:
            vte_terminal = terminal.get_vte()
            col, row = vte_terminal.get_cursor_position()
            row_start = 0
            row_end = max(row, 0)
            if Vte.get_minor_version() < 72:
                text = vte_terminal.get_text_range(
                    row_start, 0, row_end, col, lambda *args: True
                )[0]
            else:
                text = vte_terminal.get_text_range_format(
                    Vte.Format.TEXT, row_start, 0, row_end, col
                )[0]
            html = None
            if capture_mode == CAPTURE_FULL and Vte.get_minor_version() >= 72:
                try:
                    html = vte_terminal.get_text_range_format(
                        Vte.Format.HTML, row_start, 0, row_end, col
                    )[0]
                except Exception as ex:
                    dbg("%s failed to capture styled scrollback: %s" % (LAYOUT_NAME, ex))
            return (
                self._trim_restored_transcript_history(text),
                self._trim_restored_transcript_history(html),
            )
        except Exception as ex:
            dbg("%s failed to capture scrollback: %s" % (LAYOUT_NAME, ex))
            return None, None

    def _trim_restored_transcript_history(self, content):
        if not content:
            return content

        marker_index = -1
        for marker in RESTORE_SEPARATOR_MARKERS:
            index = content.rfind(marker)
            if index > marker_index:
                marker_index = index
        if marker_index < 0:
            return content

        newline_index = content.find("\n", marker_index)
        if newline_index < 0:
            return ""
        return content[newline_index + 1 :]

    def _get_terminal_cwd(self, pane_id, terminal):
        try:
            cwd = terminal.get_cwd()
            if cwd:
                self._cwd_by_pane[pane_id] = cwd
                return cwd
        except Exception as ex:
            dbg("%s failed to get cwd for %s: %s" % (LAYOUT_NAME, pane_id, ex))

        cwd = self._cwd_by_pane.get(pane_id) or getattr(terminal, "cwd", None)
        if cwd:
            return cwd
        return os.path.expanduser("~")

    def _find_resume_argv(self, terminal, include_scrollback):
        if include_scrollback:
            restore_argv = self._find_resume_argv_in_scrollback(terminal)
            if restore_argv:
                return restore_argv
        return self._find_resume_argv_from_process_tree(terminal)

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

        return self._find_resume_argv_in_text(content)

    def _find_resume_argv_in_text(self, content):
        for line in self._resume_candidate_lines(content):
            match = CODEX_RESUME_RE.search(line)
            if match:
                return ["codex", "resume", match.group("session_id")]

            match = CLAUDE_RESUME_RE.search(line)
            if match:
                return ["claude", "--resume", match.group("session_id")]
        return None

    def _resume_candidate_lines(self, content):
        lines = content.splitlines()
        for index in range(len(lines) - 1, -1, -1):
            yield lines[index]
            if index > 0:
                yield " ".join(lines[index - 1 : index + 1])
            if index > 1:
                yield " ".join(lines[index - 2 : index + 1])

    def _find_resume_argv_from_process_tree(self, terminal):
        if psutil is None or not terminal.pid:
            return None

        try:
            root = psutil.Process(terminal.pid)
            processes = [root] + root.children(recursive=True)
        except psutil.Error:
            return None

        terminal_cwd = self._safe_terminal_cwd(terminal)
        fallback_argv = None
        for process in processes:
            process_kind = self._get_process_kind(process)
            if process_kind == "codex":
                session_id = self._extract_codex_session_id(process, terminal_cwd)
                if session_id:
                    return ["codex", "resume", session_id]
            elif process_kind == "claude":
                fallback_argv = ["claude", "--continue"]
                session_id = self._extract_claude_session_id(process, terminal_cwd)
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

    def _extract_codex_session_id(self, process, cwd):
        session_id = self._extract_session_id_from_env(process, CODEX_SESSION_ENV_KEYS)
        if session_id:
            return session_id

        session_id = self._extract_codex_session_id_from_cmdline(process)
        if session_id:
            return session_id

        return self._find_recent_codex_session_id(cwd, self._safe_process_create_time(process))

    def _extract_claude_session_id(self, process, cwd):
        session_id = self._extract_session_id_from_env(process, CLAUDE_SESSION_ENV_KEYS)
        if session_id:
            return session_id

        return self._find_recent_claude_session_id(
            cwd, self._safe_process_create_time(process)
        )

    def _extract_session_id_from_env(self, process, keys):
        try:
            environ = process.environ()
        except psutil.Error:
            environ = {}

        for key in keys:
            value = environ.get(key)
            if value and UUID_RE.search(value):
                return UUID_RE.search(value).group(0)
        return None

    def _extract_codex_session_id_from_cmdline(self, process):
        try:
            cmdline = process.cmdline()
        except psutil.Error:
            cmdline = []

        for value in cmdline:
            if not any(marker in value for marker in CODEX_SESSION_PATH_MARKERS):
                continue
            match = UUID_RE.search(value)
            if match:
                return match.group(0)
        return None

    def _find_recent_codex_session_id(self, cwd, started_at):
        if not cwd:
            return None

        base_dir = os.path.expanduser("~/.codex/sessions")
        for path in self._recent_session_files(base_dir, ".jsonl", started_at):
            session_id = self._codex_session_id_from_file(path, cwd)
            if session_id:
                return session_id
        return None

    def _codex_session_id_from_file(self, path, cwd):
        filename = os.path.basename(path)
        match = UUID_RE.search(filename)
        if not match:
            return None
        session_id = match.group(0)

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                first_line = handle.readline()
            entry = json.loads(first_line)
        except (OSError, ValueError):
            return None

        payload = entry.get("payload", {})
        if entry.get("type") != "session_meta" or payload.get("cwd") != cwd:
            return None
        if payload.get("id") and payload.get("id") != session_id:
            return None
        return session_id

    def _find_recent_claude_session_id(self, cwd, started_at):
        if not cwd:
            return None

        project_dir = os.path.join(
            os.path.expanduser("~/.claude/projects"), self._claude_project_name(cwd)
        )
        for path in self._recent_session_files(project_dir, ".jsonl", started_at):
            session_id = os.path.splitext(os.path.basename(path))[0]
            if UUID_RE.fullmatch(session_id):
                return session_id
        return None

    def _recent_session_files(self, base_dir, suffix, started_at):
        try:
            min_mtime = max(0, started_at - 120) if started_at else 0
            paths = []
            for root, _dirs, files in os.walk(base_dir):
                for filename in files:
                    if not filename.endswith(suffix):
                        continue
                    path = os.path.join(root, filename)
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    if mtime >= min_mtime:
                        paths.append((mtime, path))
            paths.sort(reverse=True)
            return [path for _mtime, path in paths[:RECENT_SESSION_SCAN_LIMIT]]
        except OSError:
            return []

    def _safe_terminal_cwd(self, terminal):
        try:
            return terminal.get_cwd()
        except Exception:
            return None

    def _safe_process_create_time(self, process):
        try:
            return process.create_time()
        except psutil.Error:
            return 0

    def _claude_project_name(self, cwd):
        return os.path.realpath(cwd).replace(os.path.sep, "-")
