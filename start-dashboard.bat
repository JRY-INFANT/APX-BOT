@echo off
title ALPHA - X Private Dashboard
echo =======================================================
echo   Starting ALPHA - X Discord Web Dashboard...
echo =======================================================
timeout /t 2 /nobreak >nul
start http://localhost:5000
node server.js
pause
