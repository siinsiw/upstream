# Builds <Dest>\Upstream\Upstream.exe with PyInstaller (one folder, no console window).
param([string]$Dest = (Join-Path $PSScriptRoot "dist"))
$ErrorActionPreference = "Stop"
$venv = Join-Path $env:LOCALAPPDATA "Upstream\venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    py -3.12 -m venv $venv
    & $py -m pip install -q -r (Join-Path $PSScriptRoot "requirements.txt")
}
& $py -m pip install -q pyinstaller

$work = Join-Path $env:TEMP "upstream-build"
Push-Location $PSScriptRoot
try {
    New-Item -ItemType Directory -Force assets | Out-Null
    & $py -c "from PySide6.QtWidgets import QApplication; from upstream.ui.theme import app_icon; a = QApplication([]); app_icon().pixmap(256, 256).save('assets/upstream.ico')"
    & $py -m PyInstaller --noconfirm --windowed --name Upstream --icon (Resolve-Path assets\upstream.ico) `
        --distpath $Dest --workpath $work --specpath $work main.py
    if ($LASTEXITCODE) { throw "PyInstaller failed" }
    Write-Host "Built: $(Join-Path $Dest 'Upstream\Upstream.exe')"
}
finally {
    Pop-Location
}
