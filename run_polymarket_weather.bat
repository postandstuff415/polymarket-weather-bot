@echo off
title Polymarket Weather Bot - 24/7 Watchdog
cd /d %~dp0

:loop
echo [%date% %time%] Starting Polymarket weather bot...
python polymarket_weather_bot.py
echo [%date% %time%] Warning: Polymarket weather bot exited (Code: %errorlevel%). Auto-restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
