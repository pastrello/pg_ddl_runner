@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if defined PYTHON_CMD goto :install
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    >&2 echo ERRO: instale Python 3.10 ou superior, com venv e pip, e tente novamente.
    exit /b 1
)
:install
if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente Python local .venv...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        >&2 echo ERRO ao criar ambiente virtual.
        exit /b 1
    )
)
echo Instalando dependencias locais...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    >&2 echo ERRO ao instalar dependencias. Verifique o acesso ao PyPI.
    exit /b 1
)
call "%~dp0pgddl.cmd" --version
if errorlevel 1 exit /b 1
echo Instalacao concluida. Use: .\pgddl.cmd -h
exit /b 0
