# Usage:  .\build.ps1 -Backend cpu     (or cuda)
param([ValidateSet("cpu","cuda")][string]$Backend = "cpu")
$ErrorActionPreference = "Stop"
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller
$idx = if ($Backend -eq "cuda") { "https://abetlen.github.io/llama-cpp-python/whl/cu124" } else { "https://abetlen.github.io/llama-cpp-python/whl/cpu" }
pip install llama-cpp-python --extra-index-url $idx --only-binary=:all:
if (Test-Path "..\Reo_UI.html") { python tools\extract_avatar.py ..\Reo_UI.html }
pyinstaller reo.spec --noconfirm
Write-Host "Built: dist\Reo\Reo.exe"
