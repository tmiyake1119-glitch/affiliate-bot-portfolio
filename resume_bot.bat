@echo off
if exist "%~dp0pause.txt" (
    del "%~dp0pause.txt"
    echo Bot resumed. pause.txt deleted.
) else (
    echo Bot was not paused (pause.txt not found).
)
