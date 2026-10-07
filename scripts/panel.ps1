# TickFlow Stock Panel - background start/stop/status control (Windows)
#
# Usage:
#   .\scripts\panel.ps1 <start|stop|status|restart> [-NoReload]
#   or from repo root:  .\panel.cmd start / stop / status / restart
#
# start   : launch backend (uvicorn, port from PORT env > .env PORT > 3018) and
#           frontend (vite, FRONTEND_PORT env > 3011) as hidden background
#           processes - closing the terminal does NOT stop them.
#           Logs: logs\backend.log / logs\frontend.log   PIDs: logs\*.pid
# stop    : kill both process trees (PID file first, listening-port fallback),
#           so instances started by dev.ps1 in another terminal are stopped too.
# status  : dev backend / dev frontend / desktop instance state
# restart : stop + start
# -NoReload: run uvicorn without --reload (background 常驻时可选)
#
# 与桌面版 (app.desktop) 的互斥: 两者共用仓库 data/ 目录, 同时跑会抢数据文件。
# 桌面版运行时 panel start 会拒绝启动; 反过来桌面版自身只看 data\.desktop.lock,
# dev 模式先跑起来再双击桌面版会闪退/失败, 需先 .\panel.cmd stop。

[CmdletBinding()]
param(
    [ValidateSet('start','stop','status','restart')]
    [string]$Action = 'status',
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'

# 与 dev.ps1 相同: 强制 UTF-8, 避免子进程/控制台编码错乱
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
} catch {}

$Root        = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BackendDir  = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$EnvFile     = Join-Path $Root '.env'
$LogDir      = Join-Path $Root 'logs'
$DataDir     = Join-Path $Root 'data'
$DesktopLock = Join-Path $DataDir '.desktop.lock'
$BackendPidFile  = Join-Path $LogDir 'backend.pid'
$FrontendPidFile = Join-Path $LogDir 'frontend.pid'

# ===== 端口解析: 与 dev.ps1 完全一致的优先级 =====
function Read-DotEnvValue($Path, $Name) {
    if (-not (Test-Path $Path)) { return $null }
    $escaped = [Regex]::Escape($Name)
    foreach ($line in Get-Content $Path) {
        if ($line -match "^\s*$escaped\s*=\s*(.*?)\s*$") {
            $value = $Matches[1].Trim()
            $value = ($value -replace '\s+#.*$', '').Trim()
            if ($value.Length -ge 2 -and
                (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                 ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                return $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

$DotEnvHost  = Read-DotEnvValue $EnvFile 'HOST'
$DotEnvPort  = Read-DotEnvValue $EnvFile 'PORT'
$BindAddress = if ($env:HOST) { $env:HOST } elseif ($DotEnvHost) { $DotEnvHost } else { '0.0.0.0' }
$DisplayHost = if ($BindAddress -in @('0.0.0.0', '::')) { 'localhost' } else { $BindAddress }

if ($env:BACKEND_PORT)      { $BackendPort = [int]$env:BACKEND_PORT }
elseif ($env:PORT)          { $BackendPort = [int]$env:PORT }
elseif ($DotEnvPort)        { $BackendPort = [int]$DotEnvPort }
else                        { $BackendPort = 3018 }
$FrontendPort = if ($env:FRONTEND_PORT) { [int]$env:FRONTEND_PORT } else { 3011 }

function Log-Info($m) { Write-Host "[panel] $m" -ForegroundColor DarkGray }
function Log-Ok  ($m) { Write-Host "[panel] $m" -ForegroundColor Green }
function Log-Warn($m) { Write-Host "[panel] $m" -ForegroundColor Yellow }
function Log-Err ($m) { Write-Host "[panel] $m" -ForegroundColor Red }

function Quote([string]$s) { return "'" + ($s -replace "'", "''") + "'" }

# 监听指定端口的存活 PID 列表; 僵尸 socket (属主进程已死) 视为空闲, 同 dev.ps1
function Get-PortPids([int]$Port) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $conns) { return @() }
    $ids = @($conns.OwningProcess | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    return @($ids | Where-Object {
        try { [System.Diagnostics.Process]::GetProcessById($_) | Out-Null; $true } catch { $false }
    })
}

function Wait-PortUp([int]$Port, [int]$Seconds) {
    for ($i = 0; $i -lt ($Seconds * 2); $i++) {
        if ((Get-PortPids $Port).Count -gt 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# 桌面版单实例锁: data\.desktop.lock 内容为 PID, 进程死了视为残留锁
function Get-DesktopPid {
    if (-not (Test-Path $DesktopLock)) { return $null }
    $raw = ''
    try { $raw = (Get-Content $DesktopLock -ErrorAction Stop | Select-Object -First 1) } catch { return $null }
    $lockPid = 0
    if ([int]::TryParse("$raw".Trim(), [ref]$lockPid)) {
        try { [System.Diagnostics.Process]::GetProcessById($lockPid) | Out-Null; return $lockPid } catch { return $null }
    }
    return $null
}

# 启动一个隐藏后台进程执行 PS 命令, 日志全量追加到单文件 (超 5MB 轮转)
function Start-Detached([string]$InnerCommand, [string]$WorkDir, [string]$LogFile) {
    if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 5MB)) {
        Move-Item -Force $LogFile "$LogFile.old"
    }
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($InnerCommand)
    $enc   = [Convert]::ToBase64String($bytes)
    # EncodedCommand 避免 ArgumentList 空格/引号转义问题; PID 为包装 powershell, 停止时 taskkill /T 连带子树
    $p = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-EncodedCommand',$enc) `
            -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru
    return $p.Id
}

function Start-All {
    $desktopPid = Get-DesktopPid
    if ($desktopPid) {
        Log-Err "桌面版正在运行 (PID $desktopPid), 与开发模式共用 data\ 目录, 不能同时跑。请先关闭桌面版窗口。"
        exit 1
    }
    if (-not (Test-Path (Join-Path $BackendDir '.venv\Scripts\python.exe'))) {
        Log-Err 'backend\.venv 不存在 - 先运行 .\dev.ps1 一次完成依赖安装'
        exit 1
    }
    if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
        Log-Err 'frontend\node_modules 不存在 - 先运行 .\dev.ps1 一次'
        exit 1
    }
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

    $busy = Get-PortPids $BackendPort
    if ($busy.Count -gt 0) {
        Log-Err "端口 $BackendPort 已被占用 (PID $($busy -join ',')) - 后端可能已在运行。先执行 .\panel.cmd stop"
        exit 1
    }
    $busy = Get-PortPids $FrontendPort
    if ($busy.Count -gt 0) {
        Log-Err "端口 $FrontendPort 已被占用 (PID $($busy -join ',')) - 前端可能已在运行。先执行 .\panel.cmd stop"
        exit 1
    }

    # ---- backend ----
    Log-Info 'starting backend (detached)...'
    $py = Join-Path $BackendDir '.venv\Scripts\python.exe'
    $backendLog = Join-Path $LogDir 'backend.log'
    $reloadArg = ''
    if (-not $NoReload) { $reloadArg = ' --reload' }
    $inner = "`$env:PYTHONUNBUFFERED='1'; Set-Location -LiteralPath $(Quote $BackendDir); " +
             "& $(Quote $py) -m uvicorn app.main:app --env-file $(Quote $EnvFile)$reloadArg " +
             "--host $BindAddress --port $BackendPort *>> $(Quote $backendLog)"
    $backendPid = Start-Detached $inner $BackendDir $backendLog

    Log-Info "waiting for backend on port $BackendPort..."
    if (-not (Wait-PortUp $BackendPort 60)) {
        Log-Err "backend 未在端口 $BackendPort 就绪 - 日志: $backendLog"
        Get-Content $backendLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
        $null = & cmd /c "taskkill /F /T /PID $backendPid 2>nul"
        exit 1
    }
    Log-Ok "backend  http://${DisplayHost}:$BackendPort  (wrapper PID $backendPid)"

    # ---- frontend ----
    Log-Info 'starting frontend (detached)...'
    $frontendLog = Join-Path $LogDir 'frontend.log'
    $innerFe = "`$env:BACKEND_HOST='$BindAddress'; `$env:BACKEND_PORT='$BackendPort'; " +
               "Set-Location -LiteralPath $(Quote $FrontendDir); " +
               "& pnpm dev --host $BindAddress --port $FrontendPort *>> $(Quote $frontendLog)"
    $frontendPid = Start-Detached $innerFe $FrontendDir $frontendLog

    Log-Info "waiting for frontend on port $FrontendPort..."
    if (-not (Wait-PortUp $FrontendPort 30)) {
        Log-Err "frontend 未在端口 $FrontendPort 就绪 - 日志: $frontendLog"
        Get-Content $frontendLog -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
        $null = & cmd /c "taskkill /F /T /PID $frontendPid 2>nul"
        exit 1
    }
    Log-Ok "frontend http://${DisplayHost}:$FrontendPort  (wrapper PID $frontendPid)"

    Set-Content -Path $BackendPidFile  -Value $backendPid  -Encoding ascii
    Set-Content -Path $FrontendPidFile -Value $frontendPid -Encoding ascii

    Write-Host ''
    Log-Ok '已在后台运行 (关闭本终端不影响)。'
    Write-Host '  停止:  .\panel.cmd stop'                        -ForegroundColor DarkGray
    Write-Host '  状态:  .\panel.cmd status'                      -ForegroundColor DarkGray
    Write-Host '  日志:  logs\backend.log / logs\frontend.log'    -ForegroundColor DarkGray
}

# 停一个服务: PID 文件里的进程树优先, 端口兜底 (覆盖 dev.ps1 手工启动的实例)
function Stop-One([string]$Name, [string]$PidFile, [int]$Port) {
    $stopped = $false
    if (Test-Path $PidFile) {
        $raw = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        $targetPid = 0
        if ([int]::TryParse("$raw".Trim(), [ref]$targetPid)) {
            $alive = $false
            try { [System.Diagnostics.Process]::GetProcessById($targetPid) | Out-Null; $alive = $true } catch {}
            if ($alive) {
                Log-Info "stopping $Name (PID $targetPid process tree)..."
                $null = & cmd /c "taskkill /F /T /PID $targetPid 2>nul"
                $stopped = $true
            }
        }
        Remove-Item -Force $PidFile -ErrorAction SilentlyContinue
    }

    Start-Sleep -Milliseconds 500
    $owners = Get-PortPids $Port
    if ($owners.Count -gt 0) {
        Log-Warn "port $Port still in use (PID $($owners -join ',')), killing..."
        foreach ($op in $owners) { $null = & cmd /c "taskkill /F /T /PID $op 2>nul" }
        $stopped = $true
    }

    for ($i = 0; $i -lt 10; $i++) {
        if ((Get-PortPids $Port).Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    $left = Get-PortPids $Port
    if ($left.Count -gt 0) {
        Log-Err "$Name 停止失败: 端口 $Port 仍被 PID $($left -join ',') 占用"
        return $false
    }
    if ($stopped) { Log-Ok "$Name stopped (port $Port freed)" }
    else          { Log-Info "$Name 本来就没在运行 (port $Port idle)" }
    return $true
}

function Show-Status {
    $b = Get-PortPids $BackendPort
    if ($b.Count -gt 0) { Log-Ok  "backend  运行中  http://${DisplayHost}:$BackendPort  (PID $($b -join ','))" }
    else                { Log-Info "backend  未运行 (port $BackendPort idle)" }

    $f = Get-PortPids $FrontendPort
    if ($f.Count -gt 0) { Log-Ok  "frontend 运行中  http://${DisplayHost}:$FrontendPort  (PID $($f -join ','))" }
    else                { Log-Info "frontend 未运行 (port $FrontendPort idle)" }

    $dp = Get-DesktopPid
    if ($dp) { Log-Ok  "desktop  运行中  http://127.0.0.1:3018  (PID $dp, 关闭窗口即停止)" }
    else     { Log-Info 'desktop  未运行' }

    Write-Host ''
    Write-Host '用法: .\panel.cmd start | stop | status | restart' -ForegroundColor DarkGray
}

switch ($Action) {
    'start'   { Start-All }
    'stop'    {
        $okB = Stop-One 'backend'  $BackendPidFile  $BackendPort
        $okF = Stop-One 'frontend' $FrontendPidFile $FrontendPort
        if (-not ($okB -and $okF)) { exit 1 }
    }
    'status'  { Show-Status }
    'restart' {
        $okB = Stop-One 'backend'  $BackendPidFile  $BackendPort
        $okF = Stop-One 'frontend' $FrontendPidFile $FrontendPort
        if (-not ($okB -and $okF)) { exit 1 }
        Start-All
    }
}
