@echo off
title Kalshi Weather Bot - 24/7 Watchdog
cd /d C:\Users\MAP02\kalshi-market-maker

:loop
echo [%date% %time%] Starting weather bot...
python weather_bot.py
echo [%date% %time%] Warning: weather bot exited (Code: %errorlevel%). Auto-restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
