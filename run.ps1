# Windows launcher. Same entry as run.sh: python -m reddit_joiner
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$PSScriptRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $PSScriptRoot
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m reddit_joiner @args
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -m reddit_joiner @args
    exit $LASTEXITCODE
}

Write-Error "Python was not found. Install Python 3, then run: py -3 -m pip install -r requirements.txt"
exit 1
