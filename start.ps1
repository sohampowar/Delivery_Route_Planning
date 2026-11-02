param(
    [ValidateRange(1024, 65500)][int]$Port = 8000,
    [ValidateSet('starter', 'full')][string]$Scale = 'full'
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $Python)) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required to create the environment.' }
    }
    & $Python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    while ($Port -lt 65535) {
        $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        try {
            $Listener.Start()
            $Listener.Stop()
            break
        } catch {
            $Port++
        }
    }
    Write-Host "Starting Waypoint at http://127.0.0.1:$Port"
    & $Python -m planner serve --port $Port --scale $Scale
} finally {
    Pop-Location
}