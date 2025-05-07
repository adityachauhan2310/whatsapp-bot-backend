@echo off
cd /d D:\WhatsappBot\backend
call venv\Scripts\activate
uvicorn main:app --reload 