$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$baseUrl = 'http://127.0.0.1:8686'
$python = (Get-Command python.exe).Source

function Get-DhbHealth {
    try { Invoke-RestMethod -Uri "$baseUrl/health/dhb" -TimeoutSec 1 }
    catch { $null }
}

function Show-DhbDashboard($health) {
    if ($health.application -ne 'dhb-reup-youtube-studio' -or $health.source_root -ne $root) {
        throw 'Cong 8686 dang duoc dung boi ban DHB khac hoac ung dung khac.'
    }
    if ($health.revision -ne $expected.revision -or $health.startup_fingerprint -ne $expected.startup_fingerprint) {
        throw 'DHB dang chay ma cu. Dung ung dung khi cac luot da xong, sau do mo lai.'
    }
    Start-Process $baseUrl
}

$report = & $python -B (Join-Path $root 'launcher_preflight.py')
if ($LASTEXITCODE -ne 0) { throw 'Kiem tra moi truong Python that bai.' }
$expected = $report | ConvertFrom-Json
foreach ($message in $expected.warnings) { Write-Warning $message }

$health = Get-DhbHealth
if ($health) {
    Show-DhbDashboard $health
    exit 0
}
if (Get-NetTCPConnection -LocalPort 8686 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Cong 8686 dang duoc dung. Kiem tra tien trinh Windows.'
}

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$launch = @{
    FilePath = $python
    ArgumentList = 'app.py'
    WorkingDirectory = $root
    WindowStyle = 'Hidden'
    PassThru = $true
    RedirectStandardOutput = Join-Path $logDir 'server.out.log'
    RedirectStandardError = Join-Path $logDir 'server.error.log'
}
$process = Start-Process @launch
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $health = Get-DhbHealth
    if ($health) {
        Show-DhbDashboard $health
        exit 0
    }
    if ($process.HasExited) { break }
    Start-Sleep -Milliseconds 500
}
throw 'Khong khoi dong duoc DHB. Xem logs\server.error.log.'
