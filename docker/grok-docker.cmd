@echo off
REM Windows CreateProcess-friendly shim for grok-docker.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0grok-docker.ps1" %*
exit /b %ERRORLEVEL%
