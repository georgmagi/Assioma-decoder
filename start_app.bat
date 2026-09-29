@echo off
rem Assioma Recorder: double-click to start. No console stays open.
rem FTP, planned ride length and the heart-rate strap are set on the app's home screen.
cd /d "%~dp0"
set PY=python
set PYW=pythonw
where py >NUL 2>NUL && set PY=py -3
where pyw >NUL 2>NUL && set PYW=pyw -3
%PY% -c "import bleak, webview, garmin_fit_sdk" 2>NUL || %PY% -m pip install --user bleak pywebview garmin-fit-sdk
start "" %PYW% recorder_app.py %*
