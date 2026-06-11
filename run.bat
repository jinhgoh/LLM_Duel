@echo off
title LLM Duel
cd /d "%~dp0"
echo Starting LLM Duel...  (browser opens at http://localhost:8501)
echo Press Ctrl+C in this window to stop.
echo.
python -m streamlit run app.py
pause
