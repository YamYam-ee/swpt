# Bygger swpt.exe (PyInstaller), en zip och en MSI-installerare i dist\.
# Kräver: pip install -r requirements.txt pyinstaller   och   dotnet tool install --global wix --version 5.0.2
param([string]$Out = "dist")
$ErrorActionPreference = "Continue"   # PyInstaller loggar till stderr; fel fångas via exitkoderna
$root = $PSScriptRoot
$version = (Select-String -Path "$root\version.py" -Pattern 'VERSION = "(.+)"').Matches[0].Groups[1].Value
$work = Join-Path $env:TEMP "swpt-build"
Write-Host "Building swpt $version"

$exclude = "pyarrow scipy pandas matplotlib numpy PIL cryptography lxml tkinter IPython jedi pytest sympy".Split(" ") |
    ForEach-Object { "--exclude-module", $_ }
python -m PyInstaller "$root\swpt.pyw" --name swpt --onedir --windowed --noconfirm --clean `
    --icon "$root\swpt.ico" --add-data "$root\logo.svg;." --add-data "$root\swpt.ico;." --add-data "$root\logo.png;." `
    --collect-submodules winrt --hidden-import pymupdf @exclude `
    --distpath "$work\dist" --workpath "$work\build" --specpath $work
if ($LASTEXITCODE) { throw "PyInstaller failed" }

New-Item -ItemType Directory -Force $Out | Out-Null
$zip = Join-Path $Out "swpt-windows-x64.zip"
Compress-Archive -Path "$work\dist\swpt" -DestinationPath $zip -Force

$msi = Join-Path $Out "swpt-setup-x64.msi"
wix build "$root\installer\swpt.wxs" -arch x64 -d Version=$version -bindpath "app=$work\dist\swpt" -o $msi
if ($LASTEXITCODE) { throw "WiX failed" }
Remove-Item (Join-Path $Out "*.wixpdb") -ErrorAction SilentlyContinue
Get-ChildItem $Out | Format-Table Name, Length
