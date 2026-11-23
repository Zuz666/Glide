# Builds the Python runtime bundled with the app (desktop\build\python): the official Windows embeddable
# Python, pip, numpy (maps, geometry), edge-tts (free online speech) and the MSVC runtime DLLs. Run once before `npm run dist`: npm run python.
# Offline push-to-talk (faster-whisper and its ~200 MB of dependencies) isn't shipped: the app installs it into
# userData\pylib the first time push-to-talk needs it (main/speech.ts). -Packages adds packages for a dev build.
# The Whisper speech model isn't shipped either (the app downloads it on first use); -Models small.en puts one in
# desktop\build\models for dev runs, which the app uses before downloading.
#
# The embeddable zip ships a python312._pth that switches Python into an isolated mode (PYTHONPATH ignored, the
# script's folder not on sys.path). The engine's tools import their sibling modules and the app adds packages
# installed later (faster-whisper) through PYTHONPATH, so the ._pth is removed: the runtime then finds its standard
# library (python312.zip) and Lib\site-packages next to python.exe like a normal install.
#
# Every download is pinned and checked against its SHA-256 (computed from the exact URLs below), so a release can't
# ship a Python or a pip that changed under the same name. Changing $Version or the pip release means new hashes:
# download the file once and put its SHA-256 (Get-FileHash) here.
param(
  [string]$Version = "3.12.10",
  [string[]]$Packages = @("numpy==2.5.4", "edge-tts==7.3.1"),
  [string[]]$Models = @()
)
$ErrorActionPreference = "Stop"
# npm runs this in Windows PowerShell; started from PowerShell 7 (the release runner's shell) it inherits PowerShell
# 7's module path, and cmdlets from its own modules (Get-FileHash, Expand-Archive, Get-AuthenticodeSignature) then
# fail to load. Its own module path back, from the user and machine settings PowerShell 7 left alone.
if ($PSVersionTable.PSEdition -eq "Desktop") {
  $env:PSModulePath = (@([Environment]::GetEnvironmentVariable("PSModulePath", "User"),
    [Environment]::GetEnvironmentVariable("PSModulePath", "Machine")) | Where-Object { $_ }) -join ";"
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "build\python"
$tmp = Join-Path $root "build\.python-download"
New-Item -ItemType Directory -Force $tmp | Out-Null

$PythonSha256 = @{ "3.12.10" = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3" }
# pip itself as a wheel from PyPI (a pinned release, not bootstrap.pypa.io/get-pip.py, which changes with every pip)
$PipWheel = "pip-26.2.1-py3-none-any.whl"
$PipUrl = "https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/$PipWheel"
$PipSha256 = "71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e"

# Downloads $url to $file unless it's there, then checks the SHA-256; a mismatch deletes the file and stops.
function Get-Verified([string]$url, [string]$file, [string]$sha256) {
  if (-not $sha256) { throw "no SHA-256 pinned for $url (add it in fetch-python.ps1)" }
  if (-not (Test-Path $file)) { Invoke-WebRequest $url -OutFile $file }
  $sha = [Security.Cryptography.SHA256]::Create()
  $fs = [IO.File]::OpenRead($file)
  try { $got = -join ($sha.ComputeHash($fs) | ForEach-Object { $_.ToString("x2") }) } finally { $fs.Dispose(); $sha.Dispose() }
  if ($got -ne $sha256) {
    Remove-Item -Force $file
    throw "SHA-256 mismatch for $url`n  expected $sha256`n  got      $got"
  }
}

$zip = Join-Path $tmp "python-$Version-embed-amd64.zip"
Write-Host "Python $Version (embeddable)..."
Get-Verified "https://www.python.org/ftp/python/$Version/python-$Version-embed-amd64.zip" $zip $PythonSha256[$Version]
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
[IO.Compression.ZipFile]::ExtractToDirectory($zip, $out)
Get-ChildItem $out -Filter "python*._pth" | Remove-Item -Force
New-Item -ItemType Directory -Force (Join-Path $out "Lib\site-packages") | Out-Null

$py = Join-Path $out "python.exe"
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
# Pin the embedded runtime's standard library instead of another Python registered on the build machine.
$env:PYTHONHOME = $out

# a pip wheel is importable as it is, so pip runs from it (on PYTHONPATH, as `-m pip`: pip refuses to replace
# itself any other way on Windows) and installs itself without get-pip.py
$pipwhl = Join-Path $tmp $PipWheel
Get-Verified $PipUrl $pipwhl $PipSha256
$env:PYTHONPATH = $pipwhl
& $py -m pip install --no-index --no-warn-script-location --disable-pip-version-check $pipwhl
$pipCode = $LASTEXITCODE
Remove-Item Env:PYTHONPATH
if ($pipCode -ne 0) { throw "installing pip failed" }
& $py -m pip install --no-warn-script-location --disable-pip-version-check @Packages
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# The Microsoft C++ runtime that compiled packages (ctranslate2 for Whisper) import but the embeddable Python
# doesn't ship (it carries only vcruntime140*.dll): copied next to python.exe, which Microsoft allows for these
# redistributable DLLs, so speech recognition works on a fresh Windows without the Visual C++ Redistributable.
# They come from the build machine's System32 (the runner's Visual C++ Redistributable), whose version moves with
# Windows updates, so a hash can't be pinned: instead each copy must carry a valid Microsoft signature and be a
# 14.x (Visual C++ 2015-2022) build.
foreach ($dll in "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "concrt140.dll", "vcomp140.dll") {
  $sys = Join-Path $env:SystemRoot "System32\$dll"
  if (-not (Test-Path $sys)) { Write-Warning "$dll not found in System32; install the Visual C++ Redistributable on the build machine"; continue }
  $sig = Get-AuthenticodeSignature $sys
  $ver = (Get-Item $sys).VersionInfo.FileVersion
  if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation" -or $ver -notmatch "^14\.") {
    throw "$sys is not a Microsoft-signed Visual C++ 14.x runtime (signature $($sig.Status), version $ver)"
  }
  Copy-Item $sys (Join-Path $out $dll) -Force
}

# smoke test: the standard library, sqlite, ctypes, numpy, and site-packages on the path
& $py -c "import sys, sqlite3, ctypes, winsound, json, zlib, numpy; print('python', sys.version.split()[0], 'numpy', numpy.__version__); print([p for p in sys.path if 'site-packages' in p])"
if ($LASTEXITCODE -ne 0) { throw "the runtime doesn't work" }

# the Whisper speech models, as plain folders (model.bin, config.json, tokenizer...) the engine loads by path
$modelsDir = Join-Path $root "build\models"
foreach ($m in $Models) {
  $dst = Join-Path $modelsDir $m
  if (Test-Path (Join-Path $dst "model.bin")) { Write-Host "Whisper model $m already here"; continue }
  Write-Host "Downloading the Whisper model $m..."
  & $py -c "import sys; from faster_whisper.utils import download_model; print(download_model(sys.argv[1], output_dir=sys.argv[2]))" $m $dst
  if ($LASTEXITCODE -ne 0) { throw "downloading the Whisper model $m failed" }
}
if ($Models.Count) {
  & $py -c "import sys, faster_whisper; m = faster_whisper.WhisperModel(sys.argv[1], device='cpu', compute_type='int8'); print('whisper loads:', sys.argv[1])" (Join-Path $modelsDir $Models[0])
  if ($LASTEXITCODE -ne 0) { throw "the Whisper model doesn't load" }
}

Get-ChildItem $out -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
$mb = [math]::Round(((Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "Bundled Python ready: $out ($mb MB)"
