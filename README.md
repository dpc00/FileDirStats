# FileDirStats

Right-click a file or folder in the Sublime Text sidebar and choose "File/Folder Stats".
A "File/Folder Stats" window of its own (no tab bar, sidebar or minimap) shows the path, type, size, and for a folder the file and folder
counts, newest modified time and the 20 largest files with size bars.

- The folder walk runs in a background thread with `os.scandir`, and the numbers update
  once a second while it runs. "Largest files" appears when the walk is done.
- Problems (unreadable entries) and skipped links or junctions are listed in a coloured,
  timestamped log under the numbers, with counts in its heading. Links and junctions are
  not followed.
- Running it again cancels the walk in progress and reuses the same window.
- Auto refresh never takes the keyboard focus from the window you are working in.
- **Close** closes the stats window (it never closes the last window of Sublime Text).

Buttons at the top of the window: **Refresh**, **Cancel** (while a walk runs),
**Auto refresh** on or off, **Copy** (the stats as plain text) and **Close**.

Command palette:

- "FileDirStats: Stats of This File"
- "FileDirStats: Refresh" runs the latest stats again on the same paths.
- "FileDirStats: Toggle Auto Refresh" refreshes every 10 seconds (argument `seconds`)
  while the stats window is open and the previous run has finished.

## Install

Clone or copy this folder into Sublime Text's `Packages` folder (Preferences > Browse Packages).

## License

MIT, see [LICENSE](LICENSE).
