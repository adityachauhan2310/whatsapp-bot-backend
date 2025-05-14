@echo off
echo Starting WhatsApp Bot Backend...
cd /d D:\WhatsappBot\backend
call venv\Scripts\activate
echo Activated virtual environment.
echo.
echo Starting server on http://127.0.0.1:8000
echo Use Ctrl+C to stop the server
echo.
uvicorn main:app --reload --host 127.0.0.1 --port 8000 --log-level debug 