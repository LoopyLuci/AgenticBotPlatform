@echo off
rem name: sysinfo
rem description: Quick system summary with built-in commands only: OS, uptime, memory, disks, IP addresses
rem params: (none)
rem safety: read
setlocal EnableExtensions
set "FIND=%SystemRoot%\System32\find.exe"
set "FINDSTR=%SystemRoot%\System32\findstr.exe"
echo === Computer: %COMPUTERNAME%  User: %USERNAME%
for /f "tokens=2 delims==" %%a in ('wmic os get Caption /value 2^>nul ^| "%FIND%" "="') do echo OS: %%a
systeminfo | "%FINDSTR%" /b /c:"System Boot Time" /c:"Total Physical Memory" /c:"Available Physical Memory"
echo === Disks
for /f "skip=1 tokens=1-3" %%a in ('wmic logicaldisk where "DriveType=3" get DeviceID^,FreeSpace^,Size 2^>nul') do (
  if not "%%c"=="" echo %%a  free %%b bytes of %%c
)
echo === IPv4
ipconfig | "%FINDSTR%" /r /c:"IPv4"
endlocal
