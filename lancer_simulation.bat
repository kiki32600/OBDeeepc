@echo off
rem Test sans voiture : ECU simulee (ligne K ou CAN). Avec l'exe : C:\Diag\obdeeepc\obdeeepc.exe --sim
cd /d "%~dp0"
if exist "C:\Diag\obdeeepc\obdeeepc.exe" (start "" "C:\Diag\obdeeepc\obdeeepc.exe" --sim) else (py -3 obdeeepc.py --sim || python obdeeepc.py --sim)
