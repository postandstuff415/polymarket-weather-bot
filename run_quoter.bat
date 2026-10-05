@echo off
title Kalshi Market-Maker Engine - 24/7 Watchdog
cd /d C:\Users\MAP02\kalshi-market-maker

:loop
echo [%date% %time%] Initializing Autopilot Engine...
python autopilot.py
echo [%date% %time%] Warning: Engine exited (Code: %errorlevel%). Auto-restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
