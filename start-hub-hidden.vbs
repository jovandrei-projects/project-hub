' start-hub-hidden.vbs - run project-hub with no console window.
'
' A shortcut to this file ("project-hub.lnk") lives in the user's Startup
' folder, so the hub comes up at every login and --start-all brings the
' managed services up with it. Delete that shortcut to stop auto-starting.
' Remove --start-all below if only the hub itself should come up at login.
'
' Hub output goes to logs\hub.log (pythonw has no console; hub.py redirects
' stdout/stderr there itself). For a visible console instead, use
' start-hub.bat.
Option Explicit
Dim shell, fso, dir
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = dir
On Error Resume Next
shell.Run "pyw -3 """ & dir & "\hub.py"" --no-browser --start-all", 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "pythonw """ & dir & "\hub.py"" --no-browser --start-all", 0, False
End If
If Err.Number <> 0 Then
    MsgBox "project-hub: could not find pyw or pythonw on PATH.", vbExclamation
End If
