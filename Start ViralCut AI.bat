@echo off
cd /d "C:\Users\Rahul\OneDrive\Documents\VIralCut AI\Backend"
start "ViralCut Backend" cmd /k ".\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000"
timeout /t 3 /nobreak >nul
start "ViralCut Tunnel" cmd /k ""C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8000"
