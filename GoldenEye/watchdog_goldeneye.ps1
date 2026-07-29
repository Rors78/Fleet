# GoldenEye Watchdog
#
# Keeps the bot alive unattended. Designed to be run every few minutes by Task
# Scheduler, and once at logon.
#
#   - If the bot is not running (or /health is unreachable / wedged), start it.
#   - If it IS healthy, do nothing and exit quietly.
#   - Alerts to Telegram on every restart, and on repeated failure to recover.
#
# NON-INTERACTIVE BY DESIGN. Unlike restart_goldeneye.ps1 (the desktop
# shortcut), this script never calls Read-Host - a scheduled task that blocks on
# input hangs forever and silently stops protecting anything.
#
# Manual use:
#   powershell -File D:\GoldenEye\watchdog_goldeneye.ps1          # check + heal
#   powershell -File D:\GoldenEye\watchdog_goldeneye.ps1 -WhatIf  # report only

[CmdletBinding()]
param(
    [switch]$WhatIf,             # report what it WOULD do; change nothing
    [int]$GraceSeconds = 150     # how long to allow for a boot before judging
)

$ErrorActionPreference = 'Continue'

$BOT_DIR     = 'D:\GoldenEye'
$LAUNCHER    = 'C:\Users\Miner\Desktop\launch.bat'
$HEALTH_URL  = 'http://localhost:18095/health'
$LOG         = Join-Path $BOT_DIR 'output\watchdog.log'
$STATE_FILE       = Join-Path $BOT_DIR 'output\watchdog_state.json'
$PORTS       = @(18095, 18065, 18096)

# Restart-storm guard: if the bot dies immediately over and over (bad config,
# corrupt state, Kraken lockout), hammering it makes things worse and floods
# Telegram. After this many restarts inside the window, back off and alert once.
$MAX_RESTARTS_PER_WINDOW = 4
$WINDOW_MINUTES          = 60

function Log($msg, $level = 'INFO') {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $level, $msg
    Write-Host $line
    try {
        $dir = Split-Path $LOG -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        Add-Content -Path $LOG -Value $line -ErrorAction SilentlyContinue
        # Keep the log from growing without bound (trim to last 2000 lines)
        $fi = Get-Item $LOG -ErrorAction SilentlyContinue
        if ($fi -and $fi.Length -gt 1MB) {
            $tail = Get-Content $LOG -Tail 2000
            Set-Content -Path $LOG -Value $tail
        }
    } catch { }
}

function Read-State {
    try {
        if (Test-Path $STATE_FILE) { return Get-Content $STATE_FILE -Raw | ConvertFrom-Json }
    } catch { }
    return [pscustomobject]@{ restarts = @(); backoff_alerted = $false }
}

# Emoji above U+FFFF cannot be produced with [char] under Windows PowerShell
# 5.1 (16-bit chars) - it throws. Task Scheduler runs 5.1, so build them from
# surrogate pairs, which works identically in 5.1 and 7.
function Emoji([int]$codepoint) {
    if ($codepoint -le 0xFFFF) { return [string][char]$codepoint }
    $v   = $codepoint - 0x10000
    $hi  = [char](0xD800 + ($v -shr 10))
    $lo  = [char](0xDC00 + ($v -band 0x3FF))
    return "$hi$lo"
}

function Write-State($stateObj) {
    # The parameter is $stateObj, NOT $state: PowerShell variables are
    # case-insensitive, so a parameter named $state would shadow the script's
    # $STATE_FILE path variable and every write would land on a garbage filename
    # (silently disabling the restart-storm guard).
    try {
        # ${} ends the variable name explicitly - "$STATE_FILE.tmp" would otherwise
        # parse as the property access ($STATE_FILE).tmp.
        $tmp = "${STATE_FILE}.tmp"
        # .NET write, not Set-Content: -Encoding differs between PS 5.1 and 7,
        # and 5.1 rejected the parameter here outright.
        $json = $stateObj | ConvertTo-Json -Depth 5
        [System.IO.File]::WriteAllText($tmp, $json, [System.Text.UTF8Encoding]::new($false))
        Move-Item -Path $tmp -Destination $STATE_FILE -Force
    } catch { Log "state write failed: $($_.Exception.Message)" 'WARN' }
}

function Send-Alert($text) {
    # Reads creds from launch.bat so there is ONE place channel config lives.
    try {
        $token = $env:TELEGRAM_BOT_TOKEN
        $chat  = $env:TELEGRAM_CHAT_ID
        if ((-not $token -or -not $chat) -and (Test-Path $LAUNCHER)) {
            foreach ($line in (Get-Content $LAUNCHER)) {
                if ($line -match '^\s*set\s+TELEGRAM_BOT_TOKEN=(.+)\s*$') { $token = $Matches[1].Trim() }
                if ($line -match '^\s*set\s+TELEGRAM_CHAT_ID=(.+)\s*$')   { $chat  = $Matches[1].Trim() }
            }
        }
        if (-not $token -or -not $chat) { Log 'no telegram creds - alert skipped' 'WARN'; return }
        $body = @{ chat_id = $chat; text = $text; parse_mode = 'Markdown' }
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -Method Post -Body $body -TimeoutSec 20 | Out-Null
    } catch { Log "alert send failed: $($_.Exception.Message)" 'WARN' }
}

function Get-BotProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*goldeneye.py*' })
}

function Test-BotHealth {
    <# Returns: Healthy | Starting | Degraded | Unreachable  #>
    try {
        $h = Invoke-RestMethod -Uri $HEALTH_URL -TimeoutSec 8
        if (-not $h.status) { return 'Unreachable' }
        if ($h.status -eq 'starting') { return 'Starting' }
        # A wedged thread is worse than a clean crash: the process is up so
        # nothing looks wrong, but it has stopped doing its job.
        if ($h.stale_threads -and $h.stale_threads.Count -gt 0) {
            Log "stale threads: $($h.stale_threads -join ', ')" 'WARN'
            return 'Degraded'
        }
        $script:LastHealth = $h
        return 'Healthy'
    } catch { return 'Unreachable' }
}

function Stop-Bot {
    Log 'stopping existing bot processes/port holders'
    $procs = Get-BotProcesses
    $kill = @($procs | ForEach-Object { $_.ProcessId })
    # Host consoles + anything squatting on our ports
    foreach ($b in $procs) {
        $par = Get-CimInstance Win32_Process -Filter "ProcessId = $($b.ParentProcessId)" -ErrorAction SilentlyContinue
        if ($par -and $par.Name -eq 'cmd.exe' -and $par.CommandLine -like '*launch*') { $kill += $par.ProcessId }
    }
    foreach ($p in $PORTS) {
        foreach ($c in (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)) {
            $kill += $c.OwningProcess
        }
    }
    $kill = $kill | Sort-Object -Unique
    foreach ($procId in $kill) {
        try { Stop-Process -Id $procId -Force -ErrorAction Stop; Log "  killed PID $procId" } catch { }
    }
    # Wait for ports to actually release before restarting
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $held = @($PORTS | Where-Object { Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue })
        if ($held.Count -eq 0) { return $true }
        Start-Sleep -Seconds 1
    }
    Log 'ports still held after 30s' 'WARN'
    return $false
}

function Start-Bot {
    if (-not (Test-Path $LAUNCHER)) { Log "launcher missing: $LAUNCHER" 'ERROR'; return $false }
    # Clear partial writes left by a hard kill (same as the manual restart does)
    try {
        Get-ChildItem -Path (Join-Path $BOT_DIR 'output') -Filter '*.tmp' -Recurse -File -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue; Log "  cleared $($_.Name)" }
    } catch { }
    Log 'starting bot via launcher'
    Start-Process -FilePath $LAUNCHER -WorkingDirectory $BOT_DIR -WindowStyle Minimized
    # Wait for a real health snapshot, not just a bound port
    $deadline = (Get-Date).AddSeconds($GraceSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $s = Test-BotHealth
        if ($s -eq 'Healthy') { return $true }
    }
    return $false
}

# ---------------------------------------------------------------- MAIN
$status = Test-BotHealth
$procs  = Get-BotProcesses

if ($status -eq 'Healthy') {
    # Quiet success - the common case. Logged at low volume so the file stays
    # readable; nothing sent to Telegram.
    $h = $script:LastHealth
    Log ("healthy - {0} mode, {1}/{2} threads, {3} positions, kraken {4}" -f `
         $h.trading_mode, $h.threads_healthy, $h.total_threads, $h.open_positions, $h.kraken_api)
    # Recovery notice: if we had been in backoff, say we're better now.
    $state = Read-State
    if ($state.backoff_alerted) {
        Send-Alert "$(Emoji 0x2705) *GoldenEye recovered* - bot is healthy again ($($h.threads_healthy)/$($h.total_threads) threads)."
        $state.backoff_alerted = $false
        Write-State $state
    }
    exit 0
}

if ($status -eq 'Starting') {
    Log 'bot is still starting - leaving it alone'
    exit 0
}

Log "bot needs intervention (status=$status, processes=$($procs.Count))" 'WARN'

if ($WhatIf) {
    Log 'WhatIf: would restart the bot now'
    exit 0
}

# --- restart-storm guard -----------------------------------------------------
$state = Read-State
$cutoff = (Get-Date).AddMinutes(-$WINDOW_MINUTES)
$recent = @($state.restarts | Where-Object { $_ -and ([datetime]$_) -gt $cutoff })

if ($recent.Count -ge $MAX_RESTARTS_PER_WINDOW) {
    Log "backoff: $($recent.Count) restarts in $WINDOW_MINUTES min - not restarting again" 'ERROR'
    if (-not $state.backoff_alerted) {
        Send-Alert ("$(Emoji 0x1F534) *GoldenEye is failing to stay up*`n" +
                    "$($recent.Count) restart attempts in the last $WINDOW_MINUTES minutes. " +
                    "The watchdog has stopped retrying to avoid a crash loop.`n" +
                    "Needs a look: $LOG")
        $state.backoff_alerted = $true
        $state.restarts = $recent
        Write-State $state
    }
    exit 1
}

# --- heal --------------------------------------------------------------------
if ($procs.Count -gt 0 -or $status -eq 'Degraded') { Stop-Bot | Out-Null }

$ok = Start-Bot
$recent += (Get-Date).ToString('o')
$state.restarts = $recent
# A successful restart ends any backoff episode immediately - otherwise the
# flag stays set and the "recovered" notice waits for the next cycle.
if ($ok) { $state.backoff_alerted = $false }
Write-State $state

if ($ok) {
    $h = $script:LastHealth
    Log 'restart SUCCESS'
    Send-Alert ("$(Emoji 0x1F7E1) *GoldenEye restarted*`n" +
                "The bot was $($status.ToLower()) and has been brought back up.`n" +
                "Now: $($h.threads_healthy)/$($h.total_threads) threads, " +
                "$($h.open_positions) open, kraken $($h.kraken_api).")
    exit 0
} else {
    Log 'restart FAILED - bot did not become healthy in time' 'ERROR'
    Send-Alert ("$(Emoji 0x1F534) *GoldenEye restart failed*`n" +
                "The watchdog tried to restart the bot but it did not come up " +
                "within $GraceSeconds s. Check $LOG")
    exit 1
}
