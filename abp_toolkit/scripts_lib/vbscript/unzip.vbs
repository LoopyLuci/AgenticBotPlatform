Option Explicit
' name: unzip
' description: Extract a .zip with Windows' own shell (works on machines without PowerShell or 7-Zip)
' params: ZIPFILE DESTINATION
' safety: changes
Dim fso, shell, zipPath, dest, items
If WScript.Arguments.Count < 2 Then
    WScript.Echo "Usage: unzip.vbs ZIPFILE DESTINATION"
    WScript.Quit 2
End If
Set fso = CreateObject("Scripting.FileSystemObject")
zipPath = fso.GetAbsolutePathName(WScript.Arguments(0))
dest = fso.GetAbsolutePathName(WScript.Arguments(1))
If Not fso.FileExists(zipPath) Then
    WScript.Echo "No such file: " & zipPath
    WScript.Quit 1
End If
If Not fso.FolderExists(dest) Then fso.CreateFolder dest
Set shell = CreateObject("Shell.Application")
Set items = shell.NameSpace(zipPath).Items
shell.NameSpace(dest).CopyHere items, 4 + 16
WScript.Echo "Extracted " & items.Count & " item(s) to " & dest
