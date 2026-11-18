@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\restart.ps1"
start "" http://127.0.0.1:8080
