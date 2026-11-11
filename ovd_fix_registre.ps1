# Copie la declaration J2534 de l'OpenPort vers la cle WOW6432Node attendue par OpenVehicleDiag.
# A lancer en PowerShell "Executer en tant qu'administrateur" :
#   powershell -ExecutionPolicy Bypass -File ovd_fix_registre.ps1
# Sans risque pour FORScan / Diagbox : la cle d'origine n'est pas modifiee, on ajoute seulement une copie.
$src = "HKLM:\SOFTWARE\PassThruSupport.04.04"
$dst = "HKLM:\SOFTWARE\WOW6432Node\PassThruSupport.04.04"
if (-not (Test-Path $src)) { Write-Host "Cle $src introuvable : driver OpenPort non installe ?"; exit 1 }
if (-not (Test-Path "HKLM:\SOFTWARE\WOW6432Node")) { New-Item "HKLM:\SOFTWARE\WOW6432Node" | Out-Null }
if (-not (Test-Path $dst)) { New-Item $dst | Out-Null }
Get-ChildItem $src | ForEach-Object {
    $name = $_.PSChildName
    Copy-Item -Path $_.PSPath -Destination $dst -Recurse -Force
    Write-Host "Copie : $name"
}
Get-ChildItem $dst | ForEach-Object { Get-ItemProperty $_.PSPath | Select-Object Name, FunctionLibrary }
Write-Host "Termine. Relancez OpenVehicleDiag."
