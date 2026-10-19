import heapq
import os
import stat
import threading
import time
from html import escape

import sublime
import sublime_plugin

VIEW_NAME = "File/Folder Stats"
REFRESH_MS = 1000  # how often the view redraws while a walk is running
LOG_SHOWN = 50
LARGEST_SHOWN = 20  # files listed under "Largest"
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and symlinks on Windows

AUTO_SECONDS = 10

_running = {}  # window id -> cancel Event of the run in progress
_last = {}     # window id -> (paths, list of Stats, cancel Event) of the latest run
_auto = {}     # window id -> seconds between refreshes while auto refresh is on
_tickets = {}  # window id -> token of the auto refresh timer chain that may keep running
_phantoms = {}  # view id -> PhantomSet; a phantom is dropped when its set is garbage collected


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
    _tickets.clear()
    _phantoms.clear()
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


CSS = (
    "<style>"
    "h3{margin:0 0 2px 0;color:#66d9ef;}"
    ".path{color:#75715e;font-size:11px;margin-bottom:6px;}"
    ".bar{margin:0 0 14px 0;}"
    ".btn{color:#a6e22e;text-decoration:none;font-weight:bold;}"
    ".off{color:#75715e;text-decoration:none;font-weight:bold;}"
    ".danger{color:#f92672;text-decoration:none;font-weight:bold;}"
    ".k{color:#75715e;display:inline-block;width:90px;}"
    ".v{color:#f8f8f2;}"
    ".card{margin-bottom:18px;}"
    ".sec{color:#66d9ef;margin:10px 0 4px 0;font-weight:bold;}"
    ".sz{color:#e6db74;font-weight:bold;}"
    ".fp{color:#75715e;font-size:11px;margin-bottom:5px;}"
    ".err{color:#f92672;font-size:11px;}"
    ".skip{color:#e6db74;font-size:11px;}"
    ".hint{color:#75715e;font-size:11px;margin-top:6px;}"
    "</style>"
)


def row(key, value, color=None):
    style = ' style="color:%s;"' % color if color else ""
    return '<div><span class="k">%s</span><span class="v"%s>%s</span></div>' % (key, style, escape(value))


def toolbar(running, auto):
    parts = ['<a class="btn" href="refresh">Refresh</a>']
    if running:
        parts.append('<a class="danger" href="cancel">Cancel</a>')
    if auto:
        parts.append('<a class="btn" href="auto">Auto refresh: on (%d s)</a>' % auto)
    else:
        parts.append('<a class="off" href="auto">Auto refresh: off</a>')
    parts.append('<a class="btn" href="copy">Copy</a>')
    parts.append('<a class="btn" href="close">Close</a>')
    return '<div class="bar">' + " &nbsp;&middot;&nbsp; ".join(parts) + "</div>"


def card(stats, cancelled):
    name = os.path.basename(stats.path.rstrip("\\/")) or stats.path
    h = ['<div class="card"><h3>%s</h3><div class="path">%s</div>' % (escape(name), escape(stats.path))]
    if stats.st is None:
        h.append(row("Status", "reading...", "#66d9ef"))
    else:
        if stats.is_dir:
            if cancelled and not stats.done:
                h.append(row("Status", "cancelled", "#f92672"))
            elif stats.done:
                h.append(row("Status", "done in %.1f s" % (stats.finished - stats.started), "#a6e22e"))
            else:
                h.append(row("Status", "reading... %.1f s" % (time.time() - stats.started), "#66d9ef"))
            h.append(row("Size", "%s (%d bytes)" % (human(stats.size), stats.size)))
            h.append(row("Contents", "%d files, %d folders" % (stats.files, stats.dirs)))
            h.append(row("Newest", stamp(stats.newest) if stats.newest else "-"))
        else:
            h.append(row("Type", "file (%s)" % (os.path.splitext(stats.path)[1] or "no extension")))
            h.append(row("Size", "%s (%d bytes)" % (human(stats.size), stats.size)))
        h.append(row("Modified", stamp(stats.st.st_mtime)))
        t = created(stats.st)
        if t:
            h.append(row("Created", stamp(t)))
        if stats.is_dir:
            if not stats.done and not cancelled:
                h.append('<div class="sec">Largest files</div><div class="path">shown when done</div>')
            elif stats.largest:
                h.append('<div class="sec">Largest files</div>')
                big = sorted(stats.largest, reverse=True)
                top = big[0][0] or 1
                for size, path in big:
                    n = max(1, int(28.0 * size / top))
                    h.append('<div><span class="sz">%s</span> <span style="background-color:#66d9ef;">%s</span></div>'
                             % (human(size), "&nbsp;" * n))
                    h.append('<div class="fp">%s</div>' % escape(os.path.relpath(path, stats.path)))
    log = list(stats.log)
    if log:
        errors = sum(1 for l in log if "  ERROR " in l)
        h.append('<div class="sec">Log &mdash; %d errors, %d links skipped</div>' % (errors, stats.skipped))
        if len(log) > LOG_SHOWN:
            h.append('<div class="hint">%d earlier entries not shown</div>' % (len(log) - LOG_SHOWN))
        for line in log[-LOG_SHOWN:]:
            h.append('<div class="%s">%s</div>' % ("err" if "  ERROR " in line else "skip", escape(line)))
    h.append("</div>")
    return "".join(h)


def page(all_stats, cancelled, auto):
    running = not cancelled and not all(s.done for s in all_stats)
    return ('<body style="background-color:#1e1e1e; padding:14px;">' + CSS + toolbar(running, auto)
            + "".join(card(s, cancelled) for s in all_stats) + "</body>")


def find_view(window):
    return next((v for v in window.views() if v.name() == VIEW_NAME), None)


def open_view(window, focus=True):
    """Find or create the stats tab. A new tab goes to another group when there is one, so it
    never covers the tab the user is working in; focus=False hands the keyboard back."""
    view = find_view(window)
    if view is not None:
        if focus:
            window.focus_view(view)
        return view
    group, previous = window.active_group(), window.active_view()
    view = window.new_file()
    view.set_name(VIEW_NAME)
    view.set_scratch(True)
    view.set_read_only(True)
    for key, value in (("gutter", False), ("line_numbers", False), ("word_wrap", True),
                       ("draw_white_space", "none")):
        view.settings().set(key, value)
    if window.num_groups() > 1:
        window.set_view_index(view, (group + 1) % window.num_groups(), 0)
    window.focus_view(view)  # make it the selected tab of its group
    if not focus:
        window.focus_group(group)
        if previous is not None:
            window.focus_view(previous)
    return view


def is_shown(window, view):
    """True when the stats tab is the selected tab of its group."""
    group, _ = window.get_view_index(view)
    selected = window.active_view_in_group(group)
    return selected is not None and selected.id() == view.id()


def draw(view, all_stats, cancel):
    window = view.window()
    auto = _auto.get(window.id(), 0) if window else 0
    phantom_set = _phantoms.get(view.id())
    if phantom_set is None:
        phantom_set = _phantoms[view.id()] = sublime.PhantomSet(view, "file_dir_stats")
    phantom_set.update([sublime.Phantom(sublime.Region(0, 0), page(all_stats, cancel.is_set(), auto),
                                        sublime.LAYOUT_BLOCK, lambda href: navigate(view, href))])


def navigate(view, href):
    """on_navigate handler for the buttons."""
    window = view.window()
    if window is None:
        return
    last = _last.get(window.id())
    if href == "refresh":
        window.run_command("file_dir_stats_refresh")
    elif href == "cancel" and last:
        last[2].set()
        draw(view, last[1], last[2])
    elif href == "auto":
        window.run_command("file_dir_stats_auto_refresh")
        if last:
            draw(view, last[1], last[2])
    elif href == "copy" and last:
        sublime.set_clipboard("\n\n".join(render(s, last[2].is_set()) for s in last[1]))
        sublime.status_message("FileDirStats: copied to clipboard")
    elif href == "close":
        if last:
            last[2].set()
        view.close()


class FileDirStatsCommand(sublime_plugin.WindowCommand):
    def run(self, paths=None, focus=True):
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
        self.show(all_stats, cancel, first=True, focus=focus)

    def work_all(self, all_stats, cancel):
        for s in all_stats:
            if cancel.is_set():
                break
            work(s, cancel)

    def show(self, all_stats, cancel, first=False, focus=True):
        if cancel.is_set() and not first:
            return  # a newer run or the Cancel button took over; they draw their own state
        view = open_view(self.window, focus) if first else find_view(self.window)
        if view is None:
            cancel.set()  # the tab was closed: stop walking
            return
        draw(view, all_stats, cancel)
        if all(s.done for s in all_stats):
            sublime.status_message("FileDirStats: done")
        else:
            sublime.set_timeout(lambda: self.show(all_stats, cancel), REFRESH_MS)  # redraw only

    def is_visible(self, paths=None):
        if paths:
            return True
        view = self.window.active_view()
        return bool(view and view.file_name())


class FileDirStatsRefreshCommand(sublime_plugin.WindowCommand):
    """Run the latest stats again on the same paths."""

    def run(self, focus=True):
        last = _last.get(self.window.id())
        if not last:
            sublime.status_message("FileDirStats: nothing to refresh")
            return
        self.window.run_command("file_dir_stats", {"paths": last[0], "focus": focus})

    def is_enabled(self):
        return self.window.id() in _last


class FileDirStatsAutoRefreshCommand(sublime_plugin.WindowCommand):
    """Toggle a timer that refreshes the stats while the panel is showing."""

    def run(self, seconds=AUTO_SECONDS):
        wid = self.window.id()
        if wid in _auto:
            del _auto[wid]
            _tickets.pop(wid, None)
            sublime.status_message("FileDirStats: auto refresh off")
            return
        _auto[wid] = max(1, seconds)
        ticket = _tickets[wid] = object()  # an older timer chain sees a different ticket and ends
        sublime.status_message("FileDirStats: auto refresh every %d s" % _auto[wid])
        sublime.set_timeout(lambda: self.tick(wid, ticket), _auto[wid] * 1000)

    def tick(self, wid, ticket):
        if _tickets.get(wid) is not ticket:
            return
        window = next((w for w in sublime.windows() if w.id() == wid), None)
        if window is None:
            _auto.pop(wid, None)
            _tickets.pop(wid, None)
            return
        last = _last.get(wid)
        finished = last and (last[2].is_set() or all(s.done for s in last[1]))
        view = find_view(window)
        if finished and view is not None and is_shown(window, view):
            window.run_command("file_dir_stats_refresh", {"focus": False})
        sublime.set_timeout(lambda: self.tick(wid, ticket), _auto.get(wid, AUTO_SECONDS) * 1000)

    def is_checked(self):
        return self.window.id() in _auto
