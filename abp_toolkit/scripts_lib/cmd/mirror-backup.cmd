@echo off
rem name: mirror-backup
rem description: Mirror a folder to a backup location with robocopy (restartable, keeps timestamps; /MIR also removes files deleted from the source)
rem params: SOURCE DEST [mirror|copy] - copy (default) never deletes anything in DEST
rem safety: changes
setlocal EnableExtensions
if "%~2"=="" (
  echo Usage: %~n0 SOURCE DEST [mirror^|copy]
  exit /b 2
)
set "MODE=/E"
if /i "%~3"=="mirror" set "MODE=/MIR"
robocopy "%~1" "%~2" %MODE% /Z /R:2 /W:2 /MT:8 /NP /NFL /NDL /XJ
set RC=%ERRORLEVEL%
rem robocopy: 0-7 are success codes (files copied, extra files, mismatches); 8+ are failures
if %RC% GEQ 8 (
  echo robocopy failed with code %RC%
  exit /b %RC%
)
echo Done (robocopy code %RC%).
exit /b 0
