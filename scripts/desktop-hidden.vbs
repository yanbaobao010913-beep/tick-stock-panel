' TickFlow desktop client - zero-console launcher.
' Runs the backend in a HIDDEN console (window style 0); only the
' pywebview client window is visible. Close that window to stop.
' Resolves repo paths from this script's own location (scripts\..).
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root & "\backend"
sh.Run """" & root & "\backend\.venv\Scripts\python.exe"" -m app.desktop", 0, False
