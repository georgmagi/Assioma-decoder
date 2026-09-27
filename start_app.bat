@echo off
rem Assioma Recorder (the new window): double-click to start. No console stays open.
rem Edit these: planned ride length in minutes, your FTP in W (power zones and the Z2 band;
rem leave empty for none), and HR=--hr to connect a heart-rate strap (leave empty for pedals only).
set PLAN=90
set FTP=199
set HR=--hr
cd /d "%~dp0"
set PY=python
set PYW=pythonw
where py >NUL 2>NUL && set PY=py -3
where pyw >NUL 2>NUL && set PYW=pyw -3
%PY% -c "import bleak, webview" 2>NUL || %PY% -m pip install --user bleak pywebview
set ARGS=%HR% --plan %PLAN%
if not "%FTP%"=="" set ARGS=%ARGS% --ftp %FTP%
start "" %PYW% recorder_app.py %ARGS% %*
