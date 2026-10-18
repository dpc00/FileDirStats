# FileDirStats

Right-click a file or folder in the Sublime Text sidebar and choose "File/Folder Stats".
An output panel shows the path, type, size, and for a folder the file and folder counts,
newest modified time and the five largest files. The folder walk runs in a background thread
with `os.scandir`, which returns file sizes without an extra call per file.

The command is also in the command palette ("FileDirStats: Stats of This File").
