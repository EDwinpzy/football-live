param(
    [Parameter(Mandatory=$true)][string]$Adb,
    [string]$Serial = 'emulator-5556',
    [string]$Package = 'com.qiubo.optimaltv.public',
    [string]$MatchId = '',
    [string]$ExpectedBackendHash = '',
    [int]$Minutes = 10,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$logFile = Join-Path $OutputDirectory 'logcat.txt'
$sampleFile = Join-Path $OutputDirectory 'samples.jsonl'
if (Test-Path -LiteralPath $logFile) { throw 'Use a new evidence directory for every run.' }

function Save-Screenshot([string]$Path) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $Adb
    foreach ($arg in @('-s', $Serial, 'exec-out', 'screencap', '-p')) { $psi.ArgumentList.Add($arg) }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($psi)
    $file = [IO.File]::Open($Path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $process.StandardOutput.BaseStream.CopyTo($file) }
    finally { $file.Dispose(); $process.WaitForExit() }
}
$initialPid = (& $Adb -s $Serial shell pidof $Package).Trim()
if (!$initialPid) { throw 'App is not running.' }
$installedPackage = @(& $Adb -s $Serial shell pm path $Package) | Where-Object { $_ -match '^package:.*base\.apk$' } | Select-Object -First 1
if (!$installedPackage) { throw 'Installed APK path is unavailable.' }
$installedPath = $installedPackage.Substring(8).Trim()
$installedHash = ((& $Adb -s $Serial shell sha256sum $installedPath) -split '\s+')[0]
if ($installedHash -notmatch '^[a-fA-F0-9]{64}$') { throw 'Installed APK hash is unavailable.' }
[ordered]@{
    serial=$Serial; package=$Package; matchId=$MatchId; apkSha256=$installedHash
    startedAt=[DateTimeOffset]::Now.ToString('o'); requestedMinutes=$Minutes
} | ConvertTo-Json | Out-File (Join-Path $OutputDirectory 'run.json') -Encoding utf8
$capture = Start-Process -FilePath $Adb -ArgumentList @('-s', $Serial, 'logcat', '-v', 'threadtime', '--pid', $initialPid) -WindowStyle Hidden -PassThru -RedirectStandardOutput $logFile -RedirectStandardError (Join-Path $OutputDirectory 'logcat-stderr.txt')
$timer = [Diagnostics.Stopwatch]::StartNew()
$playbackTimer = $null
try {
    do {
        $appPid = ((& $Adb -s $Serial shell pidof $Package) -join ' ').Trim()
        $logLines = @(Get-Content -LiteralPath $logFile -ErrorAction SilentlyContinue)
        if (!$playbackTimer -and ($logLines | Select-String 'engine 首帧已渲染|vlc vout established')) {
            $playbackTimer = [Diagnostics.Stopwatch]::StartNew()
            Save-Screenshot -Path (Join-Path $OutputDirectory 'first-frame.png')
        }
        $backendHash = ((& $Adb -s $Serial shell run-as $Package sha256sum files/embedded_backend/proxy.py 2>$null) -split '\s+')[0]
        $heartbeatLine = ($logLines | Select-String 'hb (vlc )?(READY|BUFFERING)' | Select-Object -Last 1).Line
        $hb = [ordered]@{ state=$null; playing=$null; positionSeconds=$null; bufferedSeconds=$null; renderedFrames=$null; droppedFrames=$null; rebufferCount=$null; liveOffsetMs=$null }
        if ($heartbeatLine -and $heartbeatLine -match 'hb (?:vlc )?(READY|BUFFERING).*playing=(true|false) pos=(-?\d+)s buf=(\d+)s dropped=(\d+) rbuf=(\d+) rendered=(-?\d+).*liveOffsetMs=(-?\d+)') {
            $hb.state = $Matches[1]
            $hb.playing = $Matches[2] -eq 'true'
            $hb.positionSeconds = [long]$Matches[3]
            $hb.bufferedSeconds = [long]$Matches[4]
            $hb.droppedFrames = [long]$Matches[5]
            $hb.rebufferCount = [long]$Matches[6]
            $hb.renderedFrames = [long]$Matches[7]
            $hb.liveOffsetMs = [long]$Matches[8]
        }
        $sample = [ordered]@{
            time = [DateTimeOffset]::Now.ToString('o')
            elapsedSeconds = [int]$timer.Elapsed.TotalSeconds
            observedPlaybackSeconds = if ($playbackTimer) { [int]$playbackTimer.Elapsed.TotalSeconds } else { 0 }
            firstFrameObserved = $null -ne $playbackTimer
            backendProxySha256 = $backendHash
            expectedBackendSha256 = $ExpectedBackendHash
            pid = $appPid
            originalPid = $initialPid
            matchId = $MatchId
            package = $Package
            serial = $Serial
            processChanged = $appPid -ne $initialPid
            rebuffers = @($logLines | Select-String 'engine rebuffer #').Count
            playbackErrors = @($logLines | Select-String 'Playback error|BehindLiveWindowException|vlc error:').Count
            sourceFailures = @($logLines | Select-String 'bootLive 无可用信号').Count
            crashes = @($logLines | Select-String "FATAL EXCEPTION|ANR in $([regex]::Escape($Package))").Count
            heartbeat = $hb
            lastHeartbeat = $heartbeatLine
        }
        $sample | ConvertTo-Json -Compress | Add-Content -LiteralPath $sampleFile -Encoding utf8
        $sample | ConvertTo-Json -Compress | Write-Output
        if ($sample.processChanged) { break }
        if ($ExpectedBackendHash -and $timer.Elapsed.TotalSeconds -ge 60 -and $backendHash -ne $ExpectedBackendHash) {
            throw 'Device backend does not match the candidate source.'
        }
        if (!$playbackTimer -and $timer.Elapsed.TotalSeconds -ge 90) { throw 'No first frame within 90 seconds.' }
        if ($playbackTimer -and $playbackTimer.Elapsed.TotalSeconds -ge $Minutes * 60) { break }
        Start-Sleep -Seconds 30
    } while ($true)
} finally {
    if (!$capture.HasExited) { Stop-Process -Id $capture.Id }
    Save-Screenshot -Path (Join-Path $OutputDirectory 'final-frame.png')
    & $Adb -s $Serial shell dumpsys meminfo $Package | Out-File (Join-Path $OutputDirectory 'meminfo.txt') -Encoding utf8
    & $Adb -s $Serial shell dumpsys gfxinfo $Package | Out-File (Join-Path $OutputDirectory 'gfxinfo.txt') -Encoding utf8
    $finalPid = ((& $Adb -s $Serial shell pidof $Package) -join ' ').Trim()
    $finalLines = @(Get-Content -LiteralPath $logFile -ErrorAction SilentlyContinue)
    $rebuffers = @($finalLines | Select-String 'engine rebuffer #').Count
    $playbackErrors = @($finalLines | Select-String 'Playback error|BehindLiveWindowException|vlc error:').Count
    $crashes = @($finalLines | Select-String "FATAL EXCEPTION|ANR in $([regex]::Escape($Package))").Count
    $backendHash = ((& $Adb -s $Serial shell run-as $Package sha256sum files/embedded_backend/proxy.py 2>$null) -split '\s+')[0]
    $observed = if ($playbackTimer) { [int]$playbackTimer.Elapsed.TotalSeconds } else { 0 }
    $heartbeats = @($finalLines | Select-String 'hb (vlc )?(READY|BUFFERING)')
    $renderedFrames = @($heartbeats | ForEach-Object {
        if ($_.Line -match 'rendered=(-?\d+)') { [long]$Matches[1] }
    })
    $firstRendered = if ($renderedFrames.Count) { $renderedFrames[0] } else { -1 }
    $lastRendered = if ($renderedFrames.Count) { $renderedFrames[-1] } else { -1 }
    $framesAdvanced = ($firstRendered -ge 0) -and ($lastRendered -gt $firstRendered)
    $pass = ($observed -ge $Minutes * 60) -and ($rebuffers -eq 0) -and ($playbackErrors -eq 0) -and
        ($crashes -eq 0) -and ($finalPid -eq $initialPid) -and $framesAdvanced -and
        (!$ExpectedBackendHash -or $backendHash -eq $ExpectedBackendHash)
    [ordered]@{
        serial=$Serial; package=$Package; matchId=$MatchId; startPid=$initialPid; finalPid=$finalPid
        observedPlaybackSeconds=$observed; requestedMinutes=$Minutes; rebuffers=$rebuffers
        playbackErrors=$playbackErrors; crashes=$crashes; firstRenderedFrames=$firstRendered
        lastRenderedFrames=$lastRendered; framesAdvanced=$framesAdvanced
        backendMatched=(!$ExpectedBackendHash -or $backendHash -eq $ExpectedBackendHash)
        pass=$pass; finishedAt=[DateTimeOffset]::Now.ToString('o')
    } | ConvertTo-Json | Out-File (Join-Path $OutputDirectory 'result.json') -Encoding utf8
}
# A pass here covers process, duration, buffering, errors and backend identity. Match identity,
# frame progress and <=5s source-relative delay still require screenshot/log/latency review.
