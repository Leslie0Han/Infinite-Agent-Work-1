$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Runner = Join-Path $Root "tools\run_server.py"
$Log = Join-Path $Root ".codex-3001-live.log"
$ErrLog = Join-Path $Root ".codex-3001-live.err.log"

Set-Location $Root
& $Python $Runner --host 127.0.0.1 --port 3001 --log $Log --err-log $ErrLog
