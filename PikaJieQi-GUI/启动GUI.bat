@echo off
cd /d "%~dp0"
if exist "D:\APPS\anaconda\Scripts\activate.bat" (
    call "D:\APPS\anaconda\Scripts\activate.bat" autogui
)
python app.py
if errorlevel 1 pause