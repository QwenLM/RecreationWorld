@echo off
cd /d "%USERPROFILE%"
set CUA_DRIVER_RS_UPDATE_CHECK=0
set CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS=86400
set CUA_DRIVER_RS_COORDINATE_SPACE=0
set CUA_DRIVER_RS_COORDINATE_SCALE=1000
"C:\Program Files\Cua\cua-driver\bin\qwen-cua-driver.exe" serve --socket "\\.\pipe\qwen-cua-driver" --no-overlay
