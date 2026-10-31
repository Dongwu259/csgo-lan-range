@echo off
title csgotool
python "%~dp0tools\csgotool.py" shell %*
if errorlevel 1 pause
