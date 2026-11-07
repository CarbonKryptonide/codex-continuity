param(
    [string]$Root = (Join-Path $env:USERPROFILE 'Codex-Continuity'),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
    [string]$Python = 'python',
    [string]$NodePath = '',
    [switch]$SkipInitialBackup
)
$ErrorActionPreference = 'Stop'
& $Python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ or pass -Python with its executable path.' }
$installArgs = @((Join-Path $PSScriptRoot 'install.py'), '--root', $Root, '--home', $CodexHome, '--configure')
if ($NodePath) { $installArgs += @('--node', $NodePath) }
& $Python @installArgs
if ($LASTEXITCODE -ne 0) { throw 'Continuity installation failed.' }
if (-not $SkipInitialBackup) {
    & $Python (Join-Path $Root 'tools\continuity.py') --root $Root --home $CodexHome init
    if ($LASTEXITCODE -ne 0) { throw 'Initial local backup failed.' }
    & $Python (Join-Path $Root 'tools\continuity.py') --root $Root --home $CodexHome prepare-switch --no-sync
    if ($LASTEXITCODE -ne 0) { throw 'Environment snapshot failed.' }
    & $Python (Join-Path $Root 'tools\continuity.py') --root $Root --home $CodexHome check --deep
    if ($LASTEXITCODE -ne 0) { throw 'Backup verification failed.' }
}
