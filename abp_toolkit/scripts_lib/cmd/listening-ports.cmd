@echo off
rem name: listening-ports
rem description: Every TCP port something is listening on, with the program's name
rem params: [PORT] - only this port
rem safety: read
setlocal EnableExtensions
for /f "tokens=2,5" %%a in ('netstat -ano -p tcp ^| findstr /r /c:"LISTENING"') do (
  for /f "tokens=1 delims=," %%p in ('tasklist /fi "PID eq %%b" /fo csv /nh 2^>nul') do (
    if "%~1"=="" (echo %%a  pid %%b  %%~p) else (echo %%a| findstr /e /c:":%~1" >nul && echo %%a  pid %%b  %%~p)
  )
)
endlocal
