@echo off
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set "PGROOT=E:\software\PostgreSQL\17"
cd /d "E:\software\pgvector-0.8.1"
nmake /F Makefile.win install
exit /b %ERRORLEVEL%
