@echo off
rem Restore gameinfo.txt to the factory SteamAppId 730 (run this if the game stops launching).
rem Usage: restore-gameinfo.cmd "<...\steamapps\common\csgo legacy\csgo>"
rem Without an argument, ask the tool where the game is: csgotool.py gamedir

setlocal
set "GAMEDIR=%~1"
if "%GAMEDIR%"=="" (
  echo Pass the game folder as the first argument, for example:
  echo   restore-gameinfo.cmd "D:\SteamLibrary\steamapps\common\csgo legacy\csgo"
  echo Find it with:  python tools\csgotool.py gamedir
  exit /b 2
)
if not exist "%GAMEDIR%\gameinfo.txt" (
  echo No gameinfo.txt under "%GAMEDIR%" - wrong folder?
  exit /b 1
)
copy /y "%~dp0gameinfo.txt.orig" "%GAMEDIR%\gameinfo.txt"
echo restored. expected sha256 af6a607e3ff53d75077b588873796387fbd1d9f44ec8ad450e8a5acad776dbaa
endlocal
