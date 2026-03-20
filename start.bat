@echo off
powershell -Command "Start-Process 'C:\Users\gcg5\OneDrive\문서\Project\Money\venv\Scripts\python.exe' -ArgumentList 'C:\Users\gcg5\OneDrive\문서\Project\Money\run.py' -WorkingDirectory 'C:\Users\gcg5\OneDrive\문서\Project\Money'"
timeout /t 4 /nobreak >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" "http://127.0.0.1:8000"
