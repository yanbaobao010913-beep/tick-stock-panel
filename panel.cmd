@echo off
rem TickFlow Stock Panel background control: start / stop / status / restart
rem Usage: panel.cmd start | stop | status | restart
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\panel.ps1" %*
