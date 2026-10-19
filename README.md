# FileDirStats

Right-click a file or folder in the Sublime Text sidebar and choose "File/Folder Stats".
A "File/Folder Stats" tab shows the path, type, size, and for a folder the file and folder
counts, newest modified time and the 20 largest files with size bars.

- The folder walk runs in a background thread with `os.scandir`, and the numbers update
  once a second while it runs. "Largest files" appears when the walk is done.
- Problems (unreadable entries) and skipped links or junctions are listed in a coloured,
  timestamped log under the numbers, with counts in its heading. Links and junctions are
  not followed.
- Running it again on the same window cancels the walk in progress.
- The tab opens in another group when there is one, and does not take the keyboard focus
  from the tab you are working in unless you started it yourself.

Buttons at the top of the tab: **Refresh**, **Cancel** (while a walk runs),
**Auto refresh** on or off, **Copy** (the stats as plain text) and **Close**.

Command palette:

- "FileDirStats: Stats of This File"
- "FileDirStats: Refresh" runs the latest stats again on the same paths.
- "FileDirStats: Toggle Auto Refresh" refreshes every 10 seconds (argument `seconds`)
  while the stats tab is the selected tab and the previous run has finished.

## Install

Clone or copy this folder into Sublime Text's `Packages` folder (Preferences > Browse Packages).

## License

MIT, see [LICENSE](LICENSE).
