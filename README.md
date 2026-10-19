# FileDirStats

Right-click a file or folder in the Sublime Text sidebar and choose "File/Folder Stats".
An output panel shows the path, type, size, and for a folder the file and folder counts,
newest modified time and the five largest files.

- The folder walk runs in a background thread with `os.scandir`, and the numbers update
  once a second while it runs. "Largest" appears when the walk is done.
- Problems (unreadable entries) and skipped links or junctions are listed in a timestamped
  log under the numbers, with counts in its header. Links and junctions are not followed.
- Running it again on the same window cancels the walk in progress.

Command palette:

- "FileDirStats: Stats of This File"
- "FileDirStats: Refresh" runs the latest stats again on the same paths.
- "FileDirStats: Toggle Auto Refresh" refreshes every 10 seconds (argument `seconds`)
  while the stats panel is open and the previous run has finished.

## Install

Clone or copy this folder into Sublime Text's `Packages` folder (Preferences > Browse Packages).

## License

MIT, see [LICENSE](LICENSE).
