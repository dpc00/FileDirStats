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
LOG_SHOWN = 50  # error lines shown
SKIPS_SHOWN = 10  # skipped-link lines shown
LARGEST_SHOWN = 20  # files listed under "Largest"
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and symlinks on Windows

AUTO_SECONDS = 10

_run = None     # the latest Run; there is one stats window, so the state is global
_auto = 0       # seconds between refreshes while auto refresh is on, 0 when off
_ticket = None  # token of the auto refresh timer chain that may keep running
_phantom_sets = {}  # view id -> PhantomSet; a phantom is dropped when its set is garbage collected


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
    global _auto, _ticket
    _auto, _ticket = 0, None
    _phantom_sets.clear()
    if _run is not None:
        _run.cancel.set()


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


def extended(path):
    """Windows: the \\\\?\\ form lets scandir go past the 260 character path limit."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + os.path.abspath(path)


def plain(path):
    """Undo extended(), for display."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def reason(err):
    """What went wrong without repeating the path, which the log line already shows."""
    winerror = getattr(err, "winerror", None)
    return "[WinError %s] %s" % (winerror, err.strerror) if winerror else str(err.strerror or err)


def split_log(log):
    """Errors first matter most: a few hundred skipped links must not push them out of view."""
    entries = list(log)
    errors = [l for l in entries if "  ERROR " in l]
    return errors, [l for l in entries if "  ERROR " not in l]


def walk(stats, cancel):
    stack = [extended(stats.path)]
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
                            stats.note("SKIP", plain(e.path), "link or junction, not followed")
                        elif e.is_dir(follow_symlinks=False):
                            stats.dirs += 1
                            stack.append(e.path)
                        else:
                            stats.add_file(st.st_size, st.st_mtime, plain(e.path))
                    except OSError as err:
                        stats.note("ERROR", plain(e.path), reason(err))
        except OSError as err:
            stats.note("ERROR", plain(folder), reason(err))


def work(stats, cancel):
    try:
        stats.st = os.stat(extended(stats.path))
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
    errors, skips = split_log(stats.log)
    if errors or skips:
        lines.append("")
        lines.append("Log (%d errors, %d links skipped):" % (len(errors), stats.skipped))
        if len(errors) > LOG_SHOWN:
            lines.append("  ... %d earlier errors not shown" % (len(errors) - LOG_SHOWN))
        lines += ["  " + l for l in errors[-LOG_SHOWN:]]
        if len(skips) > SKIPS_SHOWN:
            lines.append("  ... %d earlier skipped links not shown" % (len(skips) - SKIPS_SHOWN))
        lines += ["  " + l for l in skips[-SKIPS_SHOWN:]]
    return "\n".join(lines)


CSS = (
    "<style>"
    "h3{margin:0 0 2px 0;color:#66d9ef;}"
    ".path{color:#c8c8c0;font-size:11px;margin-bottom:6px;}"
    ".bar{margin:0 0 14px 0;}"
    ".btn{color:#a6e22e;text-decoration:none;font-weight:bold;}"
    ".off{color:#c8c8c0;text-decoration:none;font-weight:bold;}"
    ".danger{color:#ff6188;text-decoration:none;font-weight:bold;}"
    ".k{color:#c8c8c0;display:inline-block;width:90px;}"
    ".v{color:#f8f8f2;}"
    ".card{margin-bottom:18px;}"
    ".sec{color:#66d9ef;margin:10px 0 4px 0;font-weight:bold;}"
    ".sz{color:#ffd866;font-weight:bold;}"
    ".fp{color:#c8c8c0;font-size:11px;margin-bottom:5px;}"
    ".err{color:#ff6188;font-size:11px;}"
    ".skip{color:#ffd866;font-size:11px;}"
    ".hint{color:#c8c8c0;font-size:11px;margin-top:6px;}"
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
    errors, skips = split_log(stats.log)
    if errors or skips:
        h.append('<div class="sec">Log &mdash; %d errors, %d links skipped</div>' % (len(errors), stats.skipped))
        if len(errors) > LOG_SHOWN:
            h.append('<div class="hint">%d earlier errors not shown</div>' % (len(errors) - LOG_SHOWN))
        for line in errors[-LOG_SHOWN:]:
            h.append('<div class="err">%s</div>' % escape(line))
        if len(skips) > SKIPS_SHOWN:
            h.append('<div class="hint">%d earlier skipped links not shown</div>' % (len(skips) - SKIPS_SHOWN))
        for line in skips[-SKIPS_SHOWN:]:
            h.append('<div class="skip">%s</div>' % escape(line))
    h.append("</div>")
    return "".join(h)


def page(all_stats, cancelled, auto):
    running = not cancelled and not all(s.done for s in all_stats)
    return ('<body style="background-color:#1e1e1e; padding:14px;">' + CSS + toolbar(running, auto)
            + "".join(card(s, cancelled) for s in all_stats) + "</body>")


class Run:
    """One stats request: the paths, a Stats per path and the Event that stops them."""

    def __init__(self, paths):
        self.paths = list(paths)
        self.stats = [Stats(p) for p in paths]
        self.cancel = threading.Event()

    def finished(self):
        return self.cancel.is_set() or all(s.done for s in self.stats)


def walk_all(run):
    for s in run.stats:
        if run.cancel.is_set():
            break
        work(s, run.cancel)


def find_view():
    """The stats view, in whichever window it lives."""
    for window in sublime.windows():
        for view in window.views():
            if view.name() == VIEW_NAME:
                return view
    return None


def make_view(window):
    """Turn a new window into the stats window: one tab, no tab bar, sidebar or minimap."""
    view = next((v for v in window.views() if v.file_name() is None and v.size() == 0 and not v.name()), None)
    view = view or window.new_file()
    view.set_name(VIEW_NAME)
    view.set_scratch(True)
    view.set_read_only(True)
    for key, value in (("gutter", False), ("line_numbers", False), ("word_wrap", True),
                       ("draw_white_space", "none")):
        view.settings().set(key, value)
    window.set_tabs_visible(False)
    window.set_sidebar_visible(False)
    window.set_minimap_visible(False)
    return view


def open_window(then):
    """Open a new window and call then(view) once it exists; new_window returns before it does."""
    known = {w.id() for w in sublime.windows()}
    sublime.run_command("new_window")

    def poll(tries):
        fresh = [w for w in sublime.windows() if w.id() not in known]
        if fresh:
            then(make_view(fresh[0]))
        elif tries:
            sublime.set_timeout(lambda: poll(tries - 1), 50)
        else:
            sublime.status_message("FileDirStats: could not open its window")
            if _run is not None:
                _run.cancel.set()
    poll(60)


def draw(view):
    run = _run
    if run is None:
        return
    for stale in [vid for vid in _phantom_sets if vid != view.id()]:
        del _phantom_sets[stale]
    phantom_set = _phantom_sets.get(view.id())
    if phantom_set is None:
        phantom_set = _phantom_sets[view.id()] = sublime.PhantomSet(view, "file_dir_stats")
    phantom_set.update([sublime.Phantom(sublime.Region(0, 0), page(run.stats, run.cancel.is_set(), _auto),
                                        sublime.LAYOUT_BLOCK, lambda href: navigate(view, href))])


def redraw(run):
    """Redraw once a second while the walk runs; ends when a newer run takes over."""
    if run is not _run:
        return
    view = find_view()
    if view is None:
        run.cancel.set()  # the window was closed: stop walking
        return
    draw(view)
    if run.cancel.is_set():
        return
    if all(s.done for s in run.stats):
        sublime.status_message("FileDirStats: done")
    else:
        sublime.set_timeout(lambda: redraw(run), REFRESH_MS)


def navigate(view, href):
    """on_navigate handler for the buttons."""
    window = view.window()
    if window is None:
        return
    run = _run
    if href == "refresh":
        window.run_command("file_dir_stats_refresh")
    elif href == "cancel" and run:
        run.cancel.set()
        draw(view)
    elif href == "auto":
        window.run_command("file_dir_stats_auto_refresh")
    elif href == "copy" and run:
        sublime.set_clipboard("\n\n".join(render(s, run.cancel.is_set()) for s in run.stats))
        sublime.status_message("FileDirStats: copied to clipboard")
    elif href == "close":
        if run:
            run.cancel.set()
        if len(sublime.windows()) > 1:
            window.run_command("close_window")
        else:
            view.close()  # closing the only window would quit Sublime


class FileDirStatsCommand(sublime_plugin.WindowCommand):
    def run(self, paths=None, focus=True):
        global _run
        if not paths:
            view = self.window.active_view()
            name = view.file_name() if view else None
            paths = [name] if name else []
        if not paths:
            sublime.status_message("FileDirStats: nothing selected")
            return
        if _run:
            _run.cancel.set()
        run = _run = Run(paths)
        threading.Thread(target=walk_all, args=(run,), daemon=True).start()
        view = find_view()
        if view is None:
            previous = sublime.active_window()
            open_window(lambda v: self.begin(run, v, focus, previous))
        else:
            self.begin(run, view, focus, None)

    def begin(self, run, view, focus, previous):
        if run is not _run:
            return  # replaced while the window was opening
        draw(view)
        window = view.window()
        if window is not None and focus:
            window.bring_to_front()
        elif previous is not None:
            previous.bring_to_front()  # a new window takes the keyboard; give it back
        sublime.set_timeout(lambda: redraw(run), REFRESH_MS)

    def is_visible(self, paths=None):
        if paths:
            return True
        view = self.window.active_view()
        return bool(view and view.file_name())


class FileDirStatsRefreshCommand(sublime_plugin.WindowCommand):
    """Run the latest stats again on the same paths."""

    def run(self, focus=True):
        if _run is None:
            sublime.status_message("FileDirStats: nothing to refresh")
            return
        self.window.run_command("file_dir_stats", {"paths": _run.paths, "focus": focus})

    def is_enabled(self):
        return _run is not None


class FileDirStatsAutoRefreshCommand(sublime_plugin.WindowCommand):
    """Toggle a timer that refreshes the stats while the stats window is open."""

    def run(self, seconds=AUTO_SECONDS):
        global _auto, _ticket
        if _auto:
            _auto, _ticket = 0, None
            sublime.status_message("FileDirStats: auto refresh off")
        else:
            _auto = max(1, seconds)
            ticket = _ticket = object()  # an older timer chain sees a different ticket and ends
            sublime.status_message("FileDirStats: auto refresh every %d s" % _auto)
            sublime.set_timeout(lambda: self.tick(ticket), _auto * 1000)
        view = find_view()
        if view is not None:
            draw(view)  # the button shows the new state

    def tick(self, ticket):
        if _ticket is not ticket:
            return
        if _run is not None and _run.finished() and find_view() is not None:
            sublime.active_window().run_command("file_dir_stats_refresh", {"focus": False})
        sublime.set_timeout(lambda: self.tick(ticket), (_auto or AUTO_SECONDS) * 1000)

    def is_checked(self):
        return bool(_auto)
