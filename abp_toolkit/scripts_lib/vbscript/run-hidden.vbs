Option Explicit
' name: run-hidden
' description: Run a command with no window at all (for scheduled tasks and launchers), optionally waiting for it and returning its exit code
' params: COMMAND [wait]
' safety: executes
Dim sh, code
If WScript.Arguments.Count < 1 Then
    WScript.Echo "Usage: run-hidden.vbs ""COMMAND with args"" [wait]"
    WScript.Quit 2
End If
Set sh = CreateObject("WScript.Shell")
code = sh.Run(WScript.Arguments(0), 0, WScript.Arguments.Count > 1)
WScript.Quit code
