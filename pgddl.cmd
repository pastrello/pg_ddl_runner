@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONUTF8=1"
set "PGDDL_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PGDDL_PY%" (
    >&2 echo ERRO: ambiente Python nao encontrado. Execute install-windows.cmd neste diretorio.
    exit /b 1
)
"%PGDDL_PY%" "%~dp0pgddl.py" %*
exit /b %ERRORLEVEL%
