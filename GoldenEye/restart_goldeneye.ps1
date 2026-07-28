# GoldenEye Clean Restart
# Kills zombie/orphan bot processes and stale port holders, clears crash
# leftovers, starts a fresh session, waits for health, opens the dashboard.
#
# Launched by restart_goldeneye.bat (the desktop shortcut target).
#
# -Mode paper|live   overrides config.yaml's trading_mode for this run.
#                    Omit to use whatever config.yaml already says.

param(
    [ValidateSet('paper', 'live')]
    [string]$Mode
)

$ErrorActionPreference = 'Continue'

$BOT_DIR    = 'D:\GoldenEye'
$LAUNCHER   = 'C:\Users\Miner\Desktop\launch.bat'
$CONFIG     = 'D:\GoldenEye\config.yaml'
$DASHBOARD  = 'http://localhost:18065'
$PORTS      = @(18095, 18065, 18096)
$HEALTH_URL = 'http://localhost:18095/health'
$WAIT_SECS  = 120

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

Write-Host ''
Say '  ============================================' 'DarkYellow'
Say '   G O L D E N E Y E  -  Clean Restart' 'Yellow'
Say '  ============================================' 'DarkYellow'
Write-Host ''

# ---------------------------------------------------------------- 1. SURVEY
Say '[1/6] Scanning for existing GoldenEye processes...' 'Cyan'

$botProcs = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*goldeneye.py*' })

# The launcher cmd.exe windows that host those python children - killing python
# alone leaves an empty console window behind (a visible orphan).
$hostShells = @()
foreach ($b in $botProcs) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($b.ParentProcessId)" -ErrorAction SilentlyContinue
    if ($parent -and $parent.Name -eq 'cmd.exe' -and $parent.CommandLine -like '*launch*') {
        $hostShells += $parent
    }
}

$portHolders = @()
foreach ($p in $PORTS) {
    $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            $portHolders += [pscustomobject]@{ Port = $p; Pid = $c.OwningProcess; Name = $proc.ProcessName }
        }
    }
}

if ($botProcs.Count -eq 0 -and $portHolders.Count -eq 0) {
    Say '      Nothing running - clean slate.' 'DarkGray'
} else {
    foreach ($b in $botProcs) { Say "      bot process  PID $($b.ProcessId)" 'White' }
    foreach ($s in $hostShells) { Say "      host console PID $($s.ProcessId) (cmd.exe)" 'White' }
    foreach ($h in $portHolders) { Say "      port $($h.Port)    PID $($h.Pid) ($($h.Name))" 'White' }
}

# Orphan detection: a port held by a process that is NOT a goldeneye.py process
# @() is load-bearing: a single match would otherwise be a scalar, and scalar + array
# does string concatenation instead of a union.
$botPids = @($botProcs | ForEach-Object { $_.ProcessId })
$orphans = $portHolders | Where-Object { $botPids -notcontains $_.Pid }
if ($orphans.Count -gt 0) {
    Write-Host ''
    Say '      ORPHANS DETECTED - ports held by non-bot processes:' 'Red'
    foreach ($o in $orphans) { Say "        port $($o.Port) held by PID $($o.Pid) ($($o.Name))" 'Red' }
    Say '      These will be killed too (they block the restart).' 'Red'
}

# ------------------------------------------------------------------ 2. KILL
Write-Host ''
Say '[2/6] Terminating existing processes...' 'Cyan'

# Bot processes first, then their host consoles, then anything else on the ports.
$killPids = @(@($botPids) +
              @($hostShells | ForEach-Object { $_.ProcessId }) +
              @($portHolders | ForEach-Object { $_.Pid })) | Sort-Object -Unique

if ($killPids.Count -eq 0) {
    Say '      Nothing to kill.' 'DarkGray'
} else {
    # Graceful first so in-flight atomic state writes (.tmp -> os.replace) finish.
    # Console-hosted python ignores CloseMainWindow, so this is best-effort only;
    # the force pass below is what actually guarantees the kill.
    foreach ($procId in $killPids) {
        try {
            $p = Get-Process -Id $procId -ErrorAction Stop
            $p.CloseMainWindow() | Out-Null
        } catch { }
    }
    Start-Sleep -Seconds 3

    foreach ($procId in $killPids) {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p) {
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Say "      force killed PID $procId" 'Yellow'
            } catch {
                Say "      COULD NOT KILL PID $procId - $($_.Exception.Message)" 'Red'
            }
        } else {
            Say "      PID $procId exited cleanly" 'Green'
        }
    }
}

# ------------------------------------------------------- 3. VERIFY PORTS FREE
Write-Host ''
Say '[3/6] Waiting for ports to release...' 'Cyan'

$deadline = (Get-Date).AddSeconds(30)
$stuck = @()
while ((Get-Date) -lt $deadline) {
    $stuck = @()
    foreach ($p in $PORTS) {
        if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { $stuck += $p }
    }
    if ($stuck.Count -eq 0) { break }
    Start-Sleep -Milliseconds 1000
}

if ($stuck.Count -gt 0) {
    Say "      PORTS STILL HELD: $($stuck -join ', ')" 'Red'
    Say '      Aborting - a fresh bot would fail to bind.' 'Red'
    Say '      Check for a hung process, then run this again.' 'Red'
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit 1
}
Say '      All ports free (18095, 18065, 18096).' 'Green'

# ------------------------------------------------------ 4. CLEAR CRASH DEBRIS
Write-Host ''
Say '[4/6] Clearing crash leftovers...' 'Cyan'

$tmps = @(Get-ChildItem -Path (Join-Path $BOT_DIR 'output') -Filter '*.tmp' -Recurse -File -ErrorAction SilentlyContinue)
if ($tmps.Count -gt 0) {
    foreach ($t in $tmps) {
        Remove-Item $t.FullName -Force -ErrorAction SilentlyContinue
        Say "      removed partial write: $($t.Name)" 'Yellow'
    }
} else {
    Say '      No partial writes found.' 'DarkGray'
}

# Kill switch is a DELIBERATE halt - never auto-clear it, just report loudly.
$ks = Join-Path $BOT_DIR 'output\kill_switch.json'
if (Test-Path $ks) {
    Write-Host ''
    Say '      *** KILL SWITCH IS ACTIVE ***' 'Red'
    Say "      $ks" 'Red'
    Say '      The bot will start but take NO new entries until you delete it.' 'Red'
    Say '      (This is intentional - it fired on a 20% drawdown.)' 'Red'
}

# ------------------------------------------------------------ 4b. TRADING MODE
if ($Mode) {
    Write-Host ''
    Say "[4b ] Setting trading mode to $($Mode.ToUpper())..." 'Cyan'
    try {
        # config.yaml MUST stay BOM-free UTF-8: the bot opens it without an
        # explicit encoding, so a BOM (or a mangled non-ASCII char) crashes boot
        # with a cp1252 UnicodeDecodeError. PowerShell's -Encoding UTF8 writes a
        # BOM, so read/write raw bytes through .NET with a BOM-less encoder.
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $cfg = [System.IO.File]::ReadAllText($CONFIG, $utf8NoBom)
        $current = if ($cfg -match '(?m)^\s*trading_mode:\s*(\w+)') { $Matches[1] } else { 'unknown' }
        if ($current -eq $Mode) {
            Say "      Already $Mode - config unchanged." 'DarkGray'
        } else {
            $cfg = $cfg -replace '(?m)^(\s*trading_mode:\s*)\w+', "`${1}$Mode"
            [System.IO.File]::WriteAllText($CONFIG, $cfg, $utf8NoBom)

            # Verify the bot can still parse what we just wrote, before launching.
            $check = & python -c "import yaml,io,sys; sys.stdout.write(str(yaml.safe_load(io.open(r'$CONFIG',encoding='utf-8'))['trading_mode']))" 2>&1
            if ($LASTEXITCODE -ne 0 -or $check -ne $Mode) {
                throw "config.yaml failed post-write validation (got '$check')"
            }
            Say "      config.yaml: $current -> $Mode (validated)" 'Yellow'
        }
    } catch {
        Say "      FAILED to set mode: $($_.Exception.Message)" 'Red'
        Say '      Aborting - refusing to start in an unknown mode.' 'Red'
        Write-Host ''
        Read-Host 'Press Enter to close'
        exit 1
    }
}

# ----------------------------------------------------------------- 5. LAUNCH
Write-Host ''
Say '[5/6] Starting GoldenEye...' 'Cyan'

if (-not (Test-Path $LAUNCHER)) {
    Say "      LAUNCHER NOT FOUND: $LAUNCHER" 'Red'
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit 1
}

Start-Process -FilePath $LAUNCHER -WorkingDirectory $BOT_DIR
Say '      Launcher started in its own window.' 'Green'

# ------------------------------------------------------- 6. WAIT + OPEN VIEW
Write-Host ''
Say "[6/6] Waiting for the bot to come up (up to $WAIT_SECS s)..." 'Cyan'

# The HTTP server answers before the first health snapshot is built, so an
# immediate poll returns status=starting with empty fields and the pre-config
# default mode. Keep polling until a real snapshot lands.
$deadline = (Get-Date).AddSeconds($WAIT_SECS)
$health = $null
$answered = $false
while ((Get-Date) -lt $deadline) {
    try {
        $probe = Invoke-RestMethod -Uri $HEALTH_URL -TimeoutSec 3
        $answered = $true
        if ($probe.status -and $probe.status -ne 'starting' -and $probe.total_threads) {
            $health = $probe
            break
        }
        Say '      up, waiting for first health snapshot...' 'DarkGray'
    } catch { }
    Start-Sleep -Seconds 3
}

Write-Host ''
if ($null -eq $health) {
    if ($answered) {
        Say '  BOT IS UP BUT NEVER FINISHED STARTING.' 'Red'
        Say '  /health still reports "starting" - a boot thread is likely stuck.' 'Red'
        Say '  Check the launcher window for a traceback.' 'Red'
        Write-Host ''
        Say '  Opening dashboard anyway so you can watch it.' 'Yellow'
        Start-Process $DASHBOARD
    } else {
        Say '  HEALTH CHECK FAILED - bot did not answer on 18095.' 'Red'
        Say '  Check the launcher window for a startup traceback.' 'Red'
    }
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit 1
}

Say '  ============================================' 'DarkGreen'
Say '   GOLDENEYE IS UP' 'Green'
Say '  ============================================' 'DarkGreen'
Say "   status      : $($health.status)"
Say "   mode        : $($health.trading_mode)"
Say "   threads     : $($health.threads_healthy)/$($health.total_threads) healthy ($($health.threads_registered) registered)"
Say "   kraken api  : $($health.kraken_api)"
Say "   balance     : `$$($health.live_balance)"
Say "   positions   : $($health.open_positions) open / heat `$$($health.portfolio_heat)"
Say "   breaker     : $(if ($health.circuit_breaker_active) { 'TRIPPED' } else { 'ok' })"

if ($health.stale_threads -and $health.stale_threads.Count -gt 0) {
    Say "   STALE       : $($health.stale_threads -join ', ')" 'Red'
}
if ($health.kraken_api -ne 'connected') {
    Say '   NOTE: Kraken API is not connected - check keys / lockout.' 'Yellow'
}

Write-Host ''
Say 'Opening dashboard...' 'Cyan'
Start-Process $DASHBOARD

# Two consoles per restart is clutter: the launcher window owns the bot and
# shows its live output, so this one closes itself once the bot is confirmed up.
# Only failure paths above stay open (they Read-Host), because those need reading.
Write-Host ''
Say 'Done. Closing this window - the launcher window keeps the bot running.' 'DarkGray'
Start-Sleep -Seconds 4
