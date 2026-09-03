$ErrorActionPreference = "Stop"

$venv = Join-Path $env:LOCALAPPDATA "OSBATrafficCounter\venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    py -3.14 -m venv $venv
}

& $python -m pip install --upgrade pip

$cudaVersion = & $python -c "import torch; print(torch.version.cuda or '')" 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($cudaVersion -join ""))) {
    & $python -m pip install --upgrade --force-reinstall torch torchvision `
        --index-url https://download.pytorch.org/whl/cu128
}

& $python -m pip install -e ".[vision]"

& $python -m traffic_reviewer
