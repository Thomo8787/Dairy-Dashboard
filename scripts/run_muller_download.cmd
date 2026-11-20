@echo off
rem Windows Task Scheduler runs this. The log is kept beside the downloaded CSVs.
cd /d "%~dp0.."
if not exist "data\Muller_Milk_Collections_Monthly" mkdir "data\Muller_Milk_Collections_Monthly"
echo ===== %date% %time% ===== >> "data\Muller_Milk_Collections_Monthly\download.log"
".venv\Scripts\python.exe" scripts\muller_download.py --headless >> "data\Muller_Milk_Collections_Monthly\download.log" 2>&1
