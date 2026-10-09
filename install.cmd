@echo off
rem Double-click or run from a terminal: installs ChatBridge for the current user (see install.ps1 for options).
rem   install.cmd            install
rem   install.cmd -Service   install + auto-sync at logon
rem   install.cmd -Uninstall remove
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
