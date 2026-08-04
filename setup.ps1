# Dairy Dashboard - local setup (Windows PowerShell)
#
# Prerequisites: Python 3.12+ installed from https://www.python.org/downloads/
# During install, check "Add python.exe to PATH".

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "Creating virtual environment in .venv ..."
python -m venv .venv

Write-Host "Activating virtual environment ..."
& ".\.venv\Scripts\Activate.ps1"

Write-Host "Installing dependencies ..."
python -m pip install --upgrade pip
pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - update it with your credentials."
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next steps:"
Write-Host "  1. Edit .env with DATABASE_URL and Microsoft Graph credentials"
Write-Host "  2. Activate: .\.venv\Scripts\Activate.ps1"
Write-Host "  3. Run locally: python app.py"
Write-Host "  4. Open: http://localhost:5000"
