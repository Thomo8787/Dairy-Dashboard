# Builds ThomassonFarmsDashboard.exe for a desktop shortcut.
# Requires the project virtualenv.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtual environment not found. Run setup.ps1 first."
}

Write-Host "Installing PyInstaller..."
& $Python -m pip install --upgrade pyinstaller

Write-Host "Building exe..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name "ThomassonFarmsDashboard" `
    --distpath $Root `
    --workpath (Join-Path $Root "build\pyinstaller") `
    --specpath (Join-Path $Root "build") `
    (Join-Path $Root "launch_dashboard.py")

Write-Host ""
Write-Host "Done. Executable:"
Write-Host "  $Root\ThomassonFarmsDashboard.exe"
Write-Host ""
Write-Host "Right-click the exe -> Show more options -> Create shortcut,"
Write-Host "then move the shortcut to your Desktop."
