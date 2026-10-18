import os
import stat
import threading
import time

import sublime
import sublime_plugin

PANEL = "file_dir_stats"


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def walk(path):
    """Total size, file count, folder count and newest mtime under path."""
    size = files = dirs = 0
    newest = 0
    largest = []
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs += 1
                            stack.append(e.path)
                        else:
                            st = e.stat(follow_symlinks=False)
                            files += 1
                            size += st.st_size
                            newest = max(newest, st.st_mtime)
                            largest.append((st.st_size, e.path))
                    except OSError:
                        pass
        except OSError:
            pass
        if len(largest) > 2000:
            largest = sorted(largest, reverse=True)[:5]
    return size, files, dirs, newest, sorted(largest, reverse=True)[:5]


def describe(path):
    st = os.stat(path)
    lines = ["Path:      " + path]
    if stat.S_ISDIR(st.st_mode):
        size, files, dirs, newest, largest = walk(path)
        lines += [
            "Type:      folder",
            "Size:      %s (%d bytes)" % (human(size), size),
            "Contents:  %d files, %d folders" % (files, dirs),
            "Newest:    " + (stamp(newest) if newest else "-"),
            "Modified:  " + stamp(st.st_mtime),
        ]
        if largest:
            lines.append("Largest:")
            lines += ["  %9s  %s" % (human(s), os.path.relpath(p, path)) for s, p in largest]
    else:
        lines += [
            "Type:      file (%s)" % (os.path.splitext(path)[1] or "no extension"),
            "Size:      %s (%d bytes)" % (human(st.st_size), st.st_size),
            "Modified:  " + stamp(st.st_mtime),
            "Created:   " + stamp(st.st_ctime),
        ]
    return "\n".join(lines)


class FileDirStatsCommand(sublime_plugin.WindowCommand):
    def run(self, paths=None):
        if not paths:
            name = self.window.active_view().file_name() if self.window.active_view() else None
            paths = [name] if name else []
        if not paths:
            sublime.status_message("FileDirStats: nothing selected")
            return
        sublime.status_message("FileDirStats: reading...")
        threading.Thread(target=self.work, args=(paths,), daemon=True).start()

    def work(self, paths):
        parts = []
        for p in paths:
            try:
                parts.append(describe(p))
            except OSError as e:
                parts.append("Path:      %s\nError:     %s" % (p, e))
        sublime.set_timeout(lambda: self.show("\n\n".join(parts)), 0)

    def show(self, text):
        panel = self.window.create_output_panel(PANEL)
        panel.run_command("append", {"characters": text + "\n"})
        self.window.run_command("show_panel", {"panel": "output." + PANEL})
        sublime.status_message("FileDirStats: done")

    def is_visible(self, paths=None):
        return True
