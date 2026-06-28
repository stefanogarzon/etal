# Build the Windows .exe (onefile, windowed). Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# Output: dist\EtAl.exe   (asset name must end in .exe for the self-updater)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install --upgrade pip
.\.venv-build\Scripts\python.exe -m pip install -r requirements.txt pythonnet pyinstaller

$addData = @(
  "--add-data", "frontend;frontend",
  "--add-data", "packs;packs",
  "--add-data", "fields.yaml;.",
  "--add-data", "icons;icons"
)
# Bundle the shared Groq key if the maintainer placed it (git-ignored).
if (Test-Path "groq_key.txt") {
  $addData += @("--add-data", "groq_key.txt;.")
} else {
  Write-Host "NOTE: groq_key.txt not found - AI will be opt-in (users paste their own key)."
}

.\.venv-build\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name EtAl `
  --icon "icons\etal-main.ico" `
  @addData --collect-all webview app.py

Write-Host "`nBuilt: dist\EtAl.exe"
Write-Host "Requires the Microsoft Edge WebView2 runtime on the target machine (preinstalled on Windows 11)."
