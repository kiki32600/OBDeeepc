@echo off
rem Lancement en mode script (Python 3 32 bits installe). Normalement inutile : utilisez l'icone du Bureau.
cd /d "%~dp0"
py -3 obdeeepc.py %* || python obdeeepc.py %*
