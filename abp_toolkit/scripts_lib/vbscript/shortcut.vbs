Option Explicit
' name: shortcut
' description: Create a Windows shortcut (.lnk) to a program, file or folder, with optional arguments, working folder and icon
' params: TARGET LINK.lnk [ARGUMENTS] [WORKING_FOLDER] [ICON]
' safety: changes
Dim sh, lnk
If WScript.Arguments.Count < 2 Then
    WScript.Echo "Usage: shortcut.vbs TARGET LINK.lnk [ARGUMENTS] [WORKING_FOLDER] [ICON]"
    WScript.Quit 2
End If
Set sh = CreateObject("WScript.Shell")
Set lnk = sh.CreateShortcut(WScript.Arguments(1))
lnk.TargetPath = WScript.Arguments(0)
If WScript.Arguments.Count > 2 Then lnk.Arguments = WScript.Arguments(2)
If WScript.Arguments.Count > 3 Then lnk.WorkingDirectory = WScript.Arguments(3)
If WScript.Arguments.Count > 4 Then lnk.IconLocation = WScript.Arguments(4)
lnk.Save
WScript.Echo "Created " & WScript.Arguments(1)
