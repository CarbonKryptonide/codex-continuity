$ErrorActionPreference = 'Stop'
$continuityPython = @@PYTHON_PS@@
if (-not (Test-Path -LiteralPath $continuityPython -PathType Leaf)) {
    throw "Continuity Python not found: $continuityPython"
}
& $continuityPython (Join-Path $PSScriptRoot 'tools\continuity.py') --root $PSScriptRoot --home @@CODEX_HOME_PS@@ @args
exit $LASTEXITCODE
