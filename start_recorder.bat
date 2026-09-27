@echo off
rem Assioma Recorder: double-click to start. Add --hr to also connect a heart-rate strap.
cd /d "%~dp0"
set PY=python
where py >NUL 2>NUL && set PY=py -3
%PY% -c "import bleak" 2>NUL || %PY% -m pip install --user bleak
%PY% -c "import PIL" 2>NUL || %PY% -m pip install --user pillow
%PY% assioma_recorder.py %*
if errorlevel 1 pause
