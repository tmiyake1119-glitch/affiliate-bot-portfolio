@echo off
echo Starting Affiliate Bot Dashboard...
start "" http://localhost:8080
python "%~dp0dashboard\app.py"
