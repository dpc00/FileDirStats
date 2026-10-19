import heapq
import os
import stat
import threading
import time

import sublime
import sublime_plugin

PANEL = "file_dir_stats"
REFRESH_MS = 1000  # how often the panel redraws while a walk is running
LOG_SHOWN = 50
LARGEST_SHOWN = 20  # files listed under "Largest"
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and symlinks on Windows

AUTO_SECONDS = 10

_running = {}  # window id -> cancel Event of the run in progress
_last = {}     # window id -> (paths, list of Stats, cancel Event) of the latest run
_auto = {}     # window id -> seconds between refreshes while auto refresh is on


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def created(st):
    """Creation time, or None where the platform only has the inode-change time."""
    if hasattr(st, "st_birthtime"):
        return st.st_birthtime
    return st.st_ctime if os.name == "nt" else None


def created_line(st):
    t = created(st)
    return ["Created:   " + stamp(t)] if t else []


def plugin_unloaded():
    """Stop timers and walks of this module instance; a reload would otherwise leave them running."""
    _auto.clear()
    for cancel in _running.values():
        cancel.set()


class Stats:
    """Running totals for one path. The worker thread writes, the UI timer reads."""

    def __init__(self, path):
        self.path = path
        self.is_dir = False
        self.size = self.files = self.dirs = 0
        self.newest = 0
        self.largest = []  # min-heap of (size, path), at most LARGEST_SHOWN
        self.skipped = 0
        self.log = []
        self.done = False
        self.started = time.time()
        self.finished = None
        self.st = None

    def note(self, level, where, what):
        self.log.append("%s  %-5s %s: %s" % (time.strftime("%H:%M:%S"), level, where, what))

    def add_file(self, size, mtime, path):
        self.files += 1
        self.size += size
        self.newest = max(self.newest, mtime)
        if len(self.largest) < LARGEST_SHOWN:
            heapq.heappush(self.largest, (size, path))
        else:
            heapq.heappushpop(self.largest, (size, path))


def walk(stats, cancel):
    stack = [stats.path]
    while stack and not cancel.is_set():
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if cancel.is_set():
                        return
                    try:
                        st = e.stat(follow_symlinks=False)
                        if e.is_symlink() or getattr(st, "st_file_attributes", 0) & REPARSE_POINT:
                            stats.skipped += 1
                            stats.note("SKIP", e.path, "link or junction, not followed")
                        elif e.is_dir(follow_symlinks=False):
                            stats.dirs += 1
                            stack.append(e.path)
                        else:
                            stats.add_file(st.st_size, st.st_mtime, e.path)
                    except OSError as err:
                        stats.note("ERROR", e.path, err)
        except OSError as err:
            stats.note("ERROR", folder, err)


def work(stats, cancel):
    try:
        stats.st = os.stat(stats.path)
        stats.is_dir = stat.S_ISDIR(stats.st.st_mode)
        if stats.is_dir:
            walk(stats, cancel)
        else:
            stats.add_file(stats.st.st_size, stats.st.st_mtime, stats.path)
    except OSError as err:
        stats.note("ERROR", stats.path, err)
    except Exception as err:  # keep the panel honest if something unexpected breaks the walk
        stats.note("ERROR", stats.path, "%s: %r" % (type(err).__name__, err))
    stats.finished = time.time()
    stats.done = True


def render(stats, cancelled=False):
    lines = ["Path:      " + stats.path]
    if stats.st is None:
        lines.append("Status:    reading...")
    elif stats.is_dir:
        if cancelled and not stats.done:
            status = "cancelled"
        elif stats.done:
            status = "done in %.1f s" % (stats.finished - stats.started)
        else:
            status = "reading... %.1f s" % (time.time() - stats.started)
        lines += [
            "Type:      folder",
            "Status:    " + status,
            "Size:      %s (%d bytes)" % (human(stats.size), stats.size),
            "Contents:  %d files, %d folders" % (stats.files, stats.dirs),
            "Newest:    " + (stamp(stats.newest) if stats.newest else "-"),
            "Modified:  " + stamp(stats.st.st_mtime),
        ] + created_line(stats.st)
        if not stats.done and not cancelled:
            lines.append("Largest:   (shown when done)")
        elif stats.largest:
            lines.append("Largest:")
            lines += ["  %9s  %s" % (human(s), os.path.relpath(p, stats.path))
                      for s, p in sorted(stats.largest, reverse=True)]
    else:
        lines += [
            "Type:      file (%s)" % (os.path.splitext(stats.path)[1] or "no extension"),
            "Size:      %s (%d bytes)" % (human(stats.size), stats.size),
            "Modified:  " + stamp(stats.st.st_mtime),
        ] + created_line(stats.st)
    log = list(stats.log)
    errors = sum(1 for l in log if "  ERROR " in l)
    if log:
        lines.append("")
        lines.append("Log (%d errors, %d links skipped):" % (errors, stats.skipped))
        if len(log) > LOG_SHOWN:
            lines.append("  ... %d earlier entries not shown" % (len(log) - LOG_SHOWN))
        lines += ["  " + l for l in log[-LOG_SHOWN:]]
    return "\n".join(lines)


class FileDirStatsCommand(sublime_plugin.WindowCommand):
    def run(self, paths=None):
        if not paths:
            view = self.window.active_view()
            name = view.file_name() if view else None
            paths = [name] if name else []
        if not paths:
            sublime.status_message("FileDirStats: nothing selected")
            return
        old = _running.get(self.window.id())
        if old:
            old.set()
        cancel = threading.Event()
        _running[self.window.id()] = cancel
        all_stats = [Stats(p) for p in paths]
        _last[self.window.id()] = (list(paths), all_stats, cancel)
        threading.Thread(target=self.work_all, args=(all_stats, cancel), daemon=True).start()
        self.show(all_stats, cancel, first=True)

    def work_all(self, all_stats, cancel):
        for s in all_stats:
            if cancel.is_set():
                break
            work(s, cancel)

    def show(self, all_stats, cancel, first=False):
        text = "\n\n".join(render(s, cancel.is_set()) for s in all_stats)
        panel = self.window.create_output_panel(PANEL)
        panel.set_read_only(False)
        panel.run_command("select_all")
        panel.run_command("right_delete")
        panel.run_command("append", {"characters": text + "\n"})
        panel.set_read_only(True)
        if first:
            self.window.run_command("show_panel", {"panel": "output." + PANEL})
        if cancel.is_set():
            return
        if all(s.done for s in all_stats):
            sublime.status_message("FileDirStats: done")
        else:
            sublime.set_timeout(lambda: self.show(all_stats, cancel), REFRESH_MS)  # redraw only; never re-open the panel

    def is_visible(self, paths=None):
        if paths:
            return True
        view = self.window.active_view()
        return bool(view and view.file_name())


class FileDirStatsRefreshCommand(sublime_plugin.WindowCommand):
    """Run the latest stats again on the same paths."""

    def run(self):
        last = _last.get(self.window.id())
        if not last:
            sublime.status_message("FileDirStats: nothing to refresh")
            return
        self.window.run_command("file_dir_stats", {"paths": last[0]})

    def is_enabled(self):
        return self.window.id() in _last


class FileDirStatsAutoRefreshCommand(sublime_plugin.WindowCommand):
    """Toggle a timer that refreshes the stats while the panel is showing."""

    def run(self, seconds=AUTO_SECONDS):
        wid = self.window.id()
        if wid in _auto:
            del _auto[wid]
            sublime.status_message("FileDirStats: auto refresh off")
            return
        _auto[wid] = max(1, seconds)
        sublime.status_message("FileDirStats: auto refresh every %d s" % _auto[wid])
        sublime.set_timeout(lambda: self.tick(wid), _auto[wid] * 1000)

    def tick(self, wid):
        if wid not in _auto:
            return
        window = next((w for w in sublime.windows() if w.id() == wid), None)
        if window is None:
            del _auto[wid]
            return
        last = _last.get(wid)
        finished = last and (last[2].is_set() or all(s.done for s in last[1]))
        if finished and window.active_panel() == "output." + PANEL:
            window.run_command("file_dir_stats_refresh")
        sublime.set_timeout(lambda: self.tick(wid), _auto.get(wid, AUTO_SECONDS) * 1000)

    def is_checked(self):
        return self.window.id() in _auto
