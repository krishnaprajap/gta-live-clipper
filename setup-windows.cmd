@echo off
rem Double-click to install. Optional: setup-windows.cmd -DataDir D:\GTAClipper\data
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\setup.ps1" %*
pause
