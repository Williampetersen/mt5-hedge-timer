# MT5 Hedge Timer - one-time setup.
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# Creates .venv, installs requirements.txt and copies the example config/.env
# files if they do not exist yet (existing files are never overwritten).
param(
    # Optional: full path to a python.exe (3.11 or newer) to build the venv with.
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Test-PythonOk([string[]]$Cmd) {
    # Returns the interpreter path if $Cmd runs Python >= 3.11, else $null.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $exe = $Cmd[0]
        $rest = @()
        if ($Cmd.Count -gt 1) { $rest = $Cmd[1..($Cmd.Count - 1)] }
        $out = & $exe @rest -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -First 1).Trim() }
    } catch { }
    finally { $ErrorActionPreference = $old }
    return $null
}

function Find-Python {
    if ($Python) {
        $p = Test-PythonOk @($Python)
        if ($p) { return $p }
        throw "The python given with -Python ($Python) is missing or older than 3.11."
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        # 3.12 first: it is the most tested with the MetaTrader5 package.
        foreach ($v in '3.12', '3.13', '3.11', '3.14') {
            $p = Test-PythonOk @('py', "-$v")
            if ($p) { return $p }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $p = Test-PythonOk @('python')
        if ($p) { return $p }
    }
    throw "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this script again."
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

Write-Host "== MT5 Hedge Timer setup in $Root"

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    $base = Find-Python
    Write-Host "Creating .venv with $base"
    Invoke-Checked $base @('-m', 'venv', '.venv')
} else {
    Write-Host ".venv already exists - reusing it"
}

Write-Host "Installing requirements..."
Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip')
Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt')

$copies = @(
    @('config\buy.example.yaml', 'config\buy.yaml'),
    @('config\sell.example.yaml', 'config\sell.yaml'),
    @('.env.example', '.env')
)
foreach ($c in $copies) {
    if (-not (Test-Path $c[1])) {
        Copy-Item $c[0] $c[1]
        Write-Host "Created $($c[1]) from $($c[0])"
    } else {
        Write-Host "$($c[1]) already exists - left unchanged"
    }
}
foreach ($d in 'logs', 'data') {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory $d | Out-Null }
}

$ver = & $VenvPython -c "import sys; print(sys.version.split()[0])"
Write-Host ""
Write-Host "Setup complete (Python $ver in .venv)."
Write-Host "Next steps:"
Write-Host "  1. Put both MT5 passwords (and optional Telegram values) in .env"
Write-Host "  2. Edit config\buy.yaml and config\sell.yaml (login, server, terminal_path, ...)"
Write-Host "  3. Check each side:   .venv\Scripts\python.exe -m hedgebot --config config\buy.yaml --check"
Write-Host "  4. Demo test (both):  .venv\Scripts\python.exe run_pair.py --test-in 60"
