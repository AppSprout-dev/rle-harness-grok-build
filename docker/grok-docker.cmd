@echo off
REM Windows CreateProcess-friendly shim for grok-docker.ps1
REM cmd.exe %* drops quoted / large argv. The harness writes argv as UTF-8 JSON
REM and sets RLE_GROK_ARGV_JSON; do not forward %* in that case.
if defined RLE_GROK_ARGV_JSON (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0grok-docker.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0grok-docker.ps1" %*
)
exit /b %ERRORLEVEL%
