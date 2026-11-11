@echo off
rem Installe (ou reinstalle) obdeeepc depuis GitHub et cree l'icone sur le Bureau.
rem Python n'est PAS necessaire. Internet requis.
set "DEST=C:\Diag\obdeeepc"
echo Telechargement de la derniere version de obdeeepc...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $z=Join-Path $env:TEMP 'obdeeepc-win32.zip'; Invoke-WebRequest -UseBasicParsing 'https://github.com/kiki32600/eeepc/releases/latest/download/obdeeepc-win32.zip' -OutFile $z; New-Item -ItemType Directory -Force '%DEST%' | Out-Null; Expand-Archive -Force $z '%DEST%'; Remove-Item $z; $s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\obdeeepc.lnk'); $s.TargetPath='%DEST%\obdeeepc.exe'; $s.WorkingDirectory='%DEST%'; $s.Save()"
if errorlevel 1 (
  echo.
  echo ERREUR : telechargement ou installation impossible. Verifiez la connexion internet.
  pause
  exit /b 1
)
echo.
echo TERMINE : obdeeepc est installe dans %DEST%
echo Une icone "obdeeepc" a ete creee sur le Bureau. Les mises a jour seront proposees au demarrage.
pause
