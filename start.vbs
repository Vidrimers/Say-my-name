' Launch SAY MY NAME via start.bat (checks packages, then starts GUI).
Option Explicit
Dim shell, fso, dir
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "cmd /c """ & dir & "\start.bat""", 0, False
