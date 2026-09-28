Option Explicit
' name: notify
' description: Show a message box (with a title and an icon), optionally closing itself after N seconds; prints which button was pressed
' params: TEXT [TITLE] [SECONDS] [info|warning|error|question]
' safety: read
Dim sh, text, title, secs, kind, icon, answer
If WScript.Arguments.Count < 1 Then
    WScript.Echo "Usage: notify.vbs TEXT [TITLE] [SECONDS] [info|warning|error|question]"
    WScript.Quit 2
End If
text = WScript.Arguments(0)
title = "Notice"
secs = 0
kind = "info"
If WScript.Arguments.Count > 1 Then title = WScript.Arguments(1)
If WScript.Arguments.Count > 2 Then secs = CInt(WScript.Arguments(2))
If WScript.Arguments.Count > 3 Then kind = LCase(WScript.Arguments(3))
Select Case kind
    Case "warning": icon = 48
    Case "error": icon = 16
    Case "question": icon = 32 + 4
    Case Else: icon = 64
End Select
Set sh = CreateObject("WScript.Shell")
answer = sh.Popup(text, secs, title, icon)
Select Case answer
    Case -1: WScript.Echo "timeout"
    Case 6: WScript.Echo "yes"
    Case 7: WScript.Echo "no"
    Case Else: WScript.Echo "ok"
End Select
