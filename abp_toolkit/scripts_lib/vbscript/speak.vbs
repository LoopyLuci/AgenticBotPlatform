Option Explicit
' name: speak
' description: Say text aloud with the Windows speech engine, or save it as a WAV file
' params: TEXT [OUTPUT.wav] [RATE -10..10]
' safety: read
Dim voice, stream, text, outFile, rate
If WScript.Arguments.Count < 1 Then
    WScript.Echo "Usage: speak.vbs TEXT [OUTPUT.wav] [RATE]"
    WScript.Quit 2
End If
text = WScript.Arguments(0)
outFile = ""
rate = 0
If WScript.Arguments.Count > 1 Then outFile = WScript.Arguments(1)
If WScript.Arguments.Count > 2 Then rate = CInt(WScript.Arguments(2))
Set voice = CreateObject("SAPI.SpVoice")
voice.Rate = rate
If outFile <> "" Then
    Set stream = CreateObject("SAPI.SpFileStream")
    stream.Open outFile, 3
    Set voice.AudioOutputStream = stream
    voice.Speak text
    stream.Close
    WScript.Echo "Saved " & outFile
Else
    voice.Speak text
End If
