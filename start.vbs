' Launch SAY MY NAME silently: prefer project venv, then system pythonw.
Option Explicit
Dim shell, fso, dir, pyw, cmd, venvPy
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

venvPy = dir & "\venv\Scripts\pythonw.exe"
If fso.FileExists(venvPy) Then
  pyw = venvPy
Else
  pyw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe")
  If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"
  End If
End If

cmd = """" & pyw & """ """ & dir & "\app\asr_ui.py"""
shell.Run cmd, 0, False
