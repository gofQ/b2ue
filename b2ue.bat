@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem b2ue launcher for Windows. Finds Blender and runs the pipeline in the background.
rem Set B2UE_BLENDER to the full path of blender.exe to choose a specific version.
rem The window waits for a key when started without arguments (double-click) or
rem when something fails. Set B2UE_NO_PAUSE=1 to turn that off.
setlocal EnableDelayedExpansion
set "B2UE_DIR=%~dp0"
set "B2UE_CODE=0"

if defined B2UE_BLENDER goto run

for %%R in ("%ProgramFiles%" "%ProgramFiles(x86)%") do (
  if exist "%%~R\Blender Foundation" (
    for /f "delims=" %%B in ('dir /b /ad /o-n "%%~R\Blender Foundation\Blender*" 2^>nul') do (
      if not defined B2UE_BLENDER if exist "%%~R\Blender Foundation\%%B\blender.exe" set "B2UE_BLENDER=%%~R\Blender Foundation\%%B\blender.exe"
    )
  )
)
if not defined B2UE_BLENDER if exist "%ProgramFiles(x86)%\Steam\steamapps\common\Blender\blender.exe" set "B2UE_BLENDER=%ProgramFiles(x86)%\Steam\steamapps\common\Blender\blender.exe"
if not defined B2UE_BLENDER (
  where blender >nul 2>nul && set "B2UE_BLENDER=blender"
)
if not defined B2UE_BLENDER (
  echo Blender was not found. Set B2UE_BLENDER to the full path of blender.exe, for example:
  echo   set B2UE_BLENDER=C:\Program Files\Blender Foundation\Blender 5.2\blender.exe
  set "B2UE_CODE=2"
  goto done
)

:run
"%B2UE_BLENDER%" -b -q --factory-startup --python-exit-code 1 -P "%B2UE_DIR%b2ue\cli.py" -- %*
set "B2UE_CODE=%ERRORLEVEL%"

:done
if defined B2UE_NO_PAUSE exit /b %B2UE_CODE%
if "%~1"=="" goto wait
if not "%B2UE_CODE%"=="0" goto wait
exit /b %B2UE_CODE%

:wait
echo.
pause
exit /b %B2UE_CODE%
