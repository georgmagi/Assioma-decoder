@echo off
rem Assioma Recorder with a Bluetooth heart-rate strap.
rem Edit these two: planned ride length in minutes (the ride overview's axis) and your FTP in W
rem (shades the Z2 band on the power graph; leave empty for none).
set PLAN=90
set FTP=199
set ARGS=--hr --plan %PLAN%
if not "%FTP%"=="" set ARGS=%ARGS% --ftp %FTP%
call "%~dp0start_recorder.bat" %ARGS% %*
