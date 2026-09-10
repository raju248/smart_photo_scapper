$ErrorActionPreference = "Stop"
Write-Host "Starting backend in a new PowerShell window..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot\backend'; if (!(Test-Path .venv)) { python -m venv .venv }; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt; playwright install chromium; uvicorn app.main:app --reload --port 8000"

Write-Host "Starting frontend in a new PowerShell window..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot\frontend'; if (!(Test-Path node_modules)) { npm install }; npm run dev"

Write-Host "Open http://localhost:3000"
