# Targeted warm-up. Same entry as run.ps1, with --targeted.
& "$PSScriptRoot\run.ps1" --targeted @args
exit $LASTEXITCODE
