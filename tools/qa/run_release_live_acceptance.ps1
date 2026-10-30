param(
    [string]$Adb = '',
    [string[]]$Targets = @(),
    [string]$TvApk = '',
    [string]$MobileApk = '',
    [switch]$InstallApks,
    [int]$Minutes = 10,
    [int]$MatchesPerVariant = 3,
    [int]$DelayProbeSeconds = 60,
    [int]$MaxStartupSkewSeconds = 120,
    [int]$LaunchTimeoutSeconds = 180,
    [string]$ApiBase = 'http://127.0.0.1:18090',
    [string]$Python = 'python',
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$TV_PACKAGE = 'com.qiubo.optimaltv.public'
$MOBILE_PACKAGE = 'com.qiubo.optimaltv.mobile.public'
$PROBE_ENTRIES = [System.Collections.Generic.List[object]]::new()
$MONITOR_RECORDS = @()
$ACTIVE_JOBS = [System.Collections.Generic.List[object]]::new()

function Write-Utf8Text([string]$Path, [string]$Text) {
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Write-JsonFile([string]$Path, $Value, [int]$Depth = 12) {
    Write-Utf8Text $Path (($Value | ConvertTo-Json -Depth $Depth) + [Environment]::NewLine)
}

function Resolve-Executable([string]$Value, [string]$Label) {
    if (!$Value) { throw "$Label path is required." }
    if (Test-Path -LiteralPath $Value) { return (Resolve-Path -LiteralPath $Value).Path }
    $command = Get-Command $Value -ErrorAction SilentlyContinue
    if (!$command) { throw "$Label was not found: $Value" }
    return $command.Source
}

function Resolve-Adb([string]$Value) {
    if ($Value) { return Resolve-Executable $Value 'adb' }
    $candidates = @()
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe') }
    if ($env:ANDROID_HOME) { $candidates += (Join-Path $env:ANDROID_HOME 'platform-tools\adb.exe') }
    if ($env:ANDROID_SDK_ROOT) { $candidates += (Join-Path $env:ANDROID_SDK_ROOT 'platform-tools\adb.exe') }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    return Resolve-Executable 'adb' 'adb'
}

function Get-Sha256File([string]$Path) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { throw "File not found: $Path" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-EmbeddedBackendProxyHash([string]$ApkPath) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
    $apkStream = $null
    $apkZip = $null
    $backendStream = $null
    $backendZip = $null
    $proxyStream = $null
    $sha = $null
    try {
        $apkStream = [IO.File]::OpenRead($ApkPath)
        $apkZip = [IO.Compression.ZipArchive]::new($apkStream, [IO.Compression.ZipArchiveMode]::Read, $true)
        $entry = $apkZip.GetEntry('assets/backend/python-backend.zip')
        if (!$entry) { throw "APK does not contain assets/backend/python-backend.zip: $ApkPath" }
        $backendStream = [IO.MemoryStream]::new()
        $entry.Open().CopyTo($backendStream)
        $backendStream.Position = 0
        $backendZip = [IO.Compression.ZipArchive]::new($backendStream, [IO.Compression.ZipArchiveMode]::Read, $true)
        $proxy = $backendZip.GetEntry('proxy.py')
        if (!$proxy) { throw "Embedded backend does not contain proxy.py: $ApkPath" }
        $proxyStream = $proxy.Open()
        $sha = [Security.Cryptography.SHA256]::Create()
        $hash = $sha.ComputeHash($proxyStream)
        return (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        if ($proxyStream) { $proxyStream.Dispose() }
        if ($sha) { $sha.Dispose() }
        if ($backendZip) { $backendZip.Dispose() }
        if ($backendStream) { $backendStream.Dispose() }
        if ($apkZip) { $apkZip.Dispose() }
        if ($apkStream) { $apkStream.Dispose() }
    }
}

function ConvertTo-Target([string]$Spec) {
    $parts = $Spec.Split('|')
    if ($parts.Count -lt 3 -or $parts.Count -gt 4) {
        throw "Target must be serial|package|matchId or serial|package|matchId|label: $Spec"
    }
    $serial = $parts[0].Trim()
    $package = $parts[1].Trim()
    $matchId = $parts[2].Trim()
    $label = if ($parts.Count -eq 4) { $parts[3].Trim() } else { '' }
    if (!$serial) { throw "Target serial is empty: $Spec" }
    if ($matchId -notmatch '^\d+$') { throw "Target matchId must be numeric: $Spec" }
    $variant = if ($package -eq $TV_PACKAGE) { 'tv' } elseif ($package -eq $MOBILE_PACKAGE) { 'mobile' } else {
        throw "Unknown package in target: $package"
    }
    $safeId = ($serial + '-' + $matchId + '-' + $variant) -replace '[^A-Za-z0-9._-]', '-'
    return [pscustomobject][ordered]@{
        id = $safeId
        serial = $serial
        package = $package
        variant = $variant
        matchId = $matchId
        label = $label
    }
}

function Get-JobFailureText($Job) {
    if ($Job.State -eq 'Failed' -and $Job.ChildJobs.Count -gt 0) {
        return [string]$Job.ChildJobs[0].JobStateInfo.Reason
    }
    return ''
}

function Stop-TrackedJobs {
    foreach ($record in $ACTIVE_JOBS) {
        $job = $record.Job
        if ($job -and $job.State -eq 'Running') {
            try { Stop-Job -Job $job -ErrorAction SilentlyContinue } catch {}
        }
    }
}

function Update-Orchestration([hashtable]$Patch) {
    $path = Join-Path $script:OutputRoot 'orchestration.json'
    $state = [ordered]@{}
    if (Test-Path -LiteralPath $path) {
        $loaded = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        foreach ($property in $loaded.PSObject.Properties) { $state[$property.Name] = $property.Value }
    }
    foreach ($key in $Patch.Keys) { $state[$key] = $Patch[$key] }
    Write-JsonFile $path $state
}

function Invoke-Evaluator([string]$Phase, [string]$OutputName) {
    $outputPath = Join-Path $script:OutputRoot $OutputName
    $evaluatorArgs = @(
        '-B', $script:EvaluatorScript,
        '--root', $script:OutputRoot,
        '--output', $outputPath,
        '--phase', $Phase,
        '--required-minutes', [string]$Minutes,
        '--matches-per-variant', [string]$MatchesPerVariant,
        '--max-delay-seconds', '5',
        '--minimum-delay-samples', '3',
        '--max-delay-growth-seconds', '0'
    )
    & $script:PythonExe @evaluatorArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        [Console]::Error.WriteLine("Acceptance phase '$Phase' failed; see $outputPath")
    }
    return $code
}

if ($Minutes -lt 10) { throw 'Minutes must be at least 10.' }
if ($MatchesPerVariant -lt 3) { throw 'MatchesPerVariant must be at least 3.' }
if ($DelayProbeSeconds -lt 30) { throw 'DelayProbeSeconds must be at least 30.' }
if ($MaxStartupSkewSeconds -lt 30 -or $MaxStartupSkewSeconds -gt 300) {
    throw 'MaxStartupSkewSeconds must be between 30 and 300.'
}
if (!$Targets -or $Targets.Count -eq 0) { throw 'At least one serial|package|matchId target is required.' }

$Control = [ordered]@{
    requiredContinuousMinutes = $Minutes
    matchesPerVariant = $MatchesPerVariant
    delayProbeSeconds = $DelayProbeSeconds
    maxStartupSkewSeconds = $MaxStartupSkewSeconds
    apiBase = $ApiBase
    requireBothVariants = $true
    requireMatchesPerVariant = $true
    maxSourceRelativeDelaySeconds = 5
    maxDelayGrowthSeconds = 2
    strongDelaySamplesPerWindow = 3
}
$matrix = @($Targets | ForEach-Object { ConvertTo-Target $_ })
if (@($matrix.id | Select-Object -Unique).Count -ne $matrix.Count) { throw 'Target IDs must be unique.' }
if (@($matrix.serial | Select-Object -Unique).Count -ne $matrix.Count) { throw 'Every target requires an independent device serial.' }
$variantCoverage = @{}
foreach ($variant in @('tv','mobile')) {
    $variantTargets = @($matrix | Where-Object variant -eq $variant)
    if ($variantTargets.Count -eq 0) { throw "Target matrix is missing the $variant variant." }
    $variantCoverage[$variant] = @($variantTargets.matchId | Select-Object -Unique).Count
    if ($variantCoverage[$variant] -lt $MatchesPerVariant) {
        throw "$variant must cover at least $MatchesPerVariant distinct matches; found $($variantCoverage[$variant])."
    }
}
$uniqueMatches = @($matrix.matchId | Select-Object -Unique)
if ($uniqueMatches.Count -lt 3) { throw 'At least three distinct match IDs are required.' }
if ($ApiBase -notmatch '^https?://') { throw 'ApiBase must use http or https.' }

if ($ValidateOnly) {
    [ordered]@{
        validation = 'pass'
        targetCount = $matrix.Count
        uniqueMatches = $uniqueMatches
        matchesByVariant = $variantCoverage
        notes = @(
            'Device availability and APK presence are checked only in capture mode.',
            'A release pass additionally requires <=5s source-relative delay with at least three strong image matches per target per window.'
        )
    } | ConvertTo-Json -Depth 8
    exit 0
}

$AdbPath = Resolve-Adb $Adb
$PythonExe = Resolve-Executable $Python 'python'
$MonitorScript = Join-Path $PSScriptRoot 'monitor_android_live.ps1'
$CompareScript = Join-Path $PSScriptRoot 'compare_live_delay.py'
$EvaluatorScript = Join-Path $PSScriptRoot 'evaluate_live_acceptance.py'
$SourceProxy = Join-Path $PSScriptRoot '..\backend-src\proxy.py'
foreach ($required in @($MonitorScript, $CompareScript, $EvaluatorScript, $SourceProxy)) {
    if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw "Required QA file is missing: $required" }
}
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue)
if (!$ffmpeg) { throw 'ffmpeg is required for source-relative delay comparison.' }
& $PythonExe -B -c "import PIL, numpy; print('ok')" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Python PIL and numpy are required for delay comparison.' }

if (!$TvApk -or !$MobileApk) {
    throw 'Release acceptance requires -TvApk and -MobileApk so installed APK hashes and embedded backend can be pinned.'
}
$TvApk = (Resolve-Path -LiteralPath $TvApk).Path
$MobileApk = (Resolve-Path -LiteralPath $MobileApk).Path
$sourceProxyHash = Get-Sha256File $SourceProxy
$candidates = [ordered]@{
    tv = [ordered]@{
        apkFileName = (Split-Path -Leaf $TvApk)
        apkSha256 = (Get-Sha256File $TvApk)
        embeddedBackendProxySha256 = (Get-EmbeddedBackendProxyHash $TvApk)
        sourceProxySha256 = $sourceProxyHash
        backendMatchesSource = $false
    }
    mobile = [ordered]@{
        apkFileName = (Split-Path -Leaf $MobileApk)
        apkSha256 = (Get-Sha256File $MobileApk)
        embeddedBackendProxySha256 = (Get-EmbeddedBackendProxyHash $MobileApk)
        sourceProxySha256 = $sourceProxyHash
        backendMatchesSource = $false
    }
}
foreach ($variant in @('tv','mobile')) {
    $candidates[$variant].backendMatchesSource = (
        $candidates[$variant].embeddedBackendProxySha256 -eq $candidates[$variant].sourceProxySha256
    )
    if (!$candidates[$variant].backendMatchesSource) {
        throw "$variant APK embeds a stale backend/proxy.py; rebuild the APK from current source."
    }
}


function Start-MonitorJob($Target) {
    $targetDirectory = Join-Path $script:OutputRoot $Target.evidenceDirectory
    $job = Start-Job -Name ("monitor-" + $Target.id) -ArgumentList @(
        $MonitorScript, $AdbPath, $Target.serial, $Target.package, $Target.matchId,
        $script:CaptureMinutes, $targetDirectory
    ) -ScriptBlock {
        param($scriptPath, $adbPath, $serial, $packageName, $matchId, $duration, $directory)
        & $scriptPath -Adb $adbPath -Serial $serial -Package $packageName -MatchId $matchId -Minutes $duration -OutputDirectory $directory
    }
    $record = [pscustomobject]@{ Type = 'monitor'; Target = $Target; Job = $job }
    $script:MONITOR_RECORDS += $record
    $script:ACTIVE_JOBS.Add($record)
}

function Test-TargetReady($Target) {
    $directory = Join-Path $script:OutputRoot $Target.evidenceDirectory
    $sampleFile = Join-Path $directory 'samples.jsonl'
    $firstFrameFile = Join-Path $directory 'first-frame.png'
    if (!(Test-Path -LiteralPath $sampleFile) -or !(Test-Path -LiteralPath $firstFrameFile)) { return $false }
    if ((Get-Item -LiteralPath $firstFrameFile).Length -le 0) { return $false }
    return [bool](Select-String -LiteralPath $sampleFile -SimpleMatch '"firstFrameObserved":true' -Quiet)
}

function Invoke-DelayProbePhase([string]$Phase) {
    $phaseDirectory = Join-Path $script:OutputRoot (Join-Path 'delay' $Phase)
    New-Item -ItemType Directory -Force -Path $phaseDirectory | Out-Null
    $startedAt = [DateTimeOffset]::Now
    $records = @()
    $groups = @($matrix | Group-Object matchId)
    foreach ($group in $groups) {
        $matchId = [string]$group.Name
        $serials = @($group.Group | ForEach-Object { $_.serial })
        $reportPath = Join-Path $phaseDirectory ($matchId + '.json')
        $stdoutPath = Join-Path $phaseDirectory ($matchId + '.stdout.log')
        $job = Start-Job -Name ("delay-$Phase-$matchId") -ArgumentList @(
            $PythonExe, $CompareScript, $AdbPath, $ApiBase, $matchId,
            ($serials -join ';'), $DelayProbeSeconds, $reportPath, $stdoutPath
        ) -ScriptBlock {
            param($pythonExe, $compareScript, $adbPath, $apiBaseArg, $matchIdArg, $serialCsv, $secondsArg, $outputPath, $logPath)
            $serialArgs = @()
            foreach ($serial in $serialCsv.Split(';')) {
                if ($serial) { $serialArgs += @('--serial', $serial) }
            }
            $compareArgs = @(
                $compareScript,
                '--adb', $adbPath,
                '--api-base', $apiBaseArg,
                '--match-id', [string]$matchIdArg,
                '--seconds', [string]$secondsArg,
                '--output', $outputPath
            ) + $serialArgs
            $output = @(& $pythonExe @compareArgs 2>&1)
            [IO.File]::WriteAllText(
                $logPath,
                (($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) + [Environment]::NewLine,
                [Text.UTF8Encoding]::new($false)
            )
            if ($LASTEXITCODE -ne 0) {
                throw "compare_live_delay.py exited with code $LASTEXITCODE"
            }
        }
        $record = [pscustomobject]@{
            Type = 'delay'
            Phase = $Phase
            MatchId = $matchId
            Report = $reportPath
            Serials = $serials
            StartedAt = $startedAt
            Job = $job
        }
        $records += $record
        $script:ACTIVE_JOBS.Add($record)
    }

    $deadline = [DateTimeOffset]::Now.AddSeconds($DelayProbeSeconds + 180)
    while (@($records | Where-Object { $_.Job.State -eq 'Running' }).Count -gt 0) {
        if ([DateTimeOffset]::Now -gt $deadline) {
            foreach ($record in $records) {
                if ($record.Job.State -eq 'Running') { Stop-Job -Job $record.Job -ErrorAction SilentlyContinue }
            }
            throw "Delay probe phase '$Phase' timed out."
        }
        $running = @($records | Where-Object { $_.Job.State -eq 'Running' } | ForEach-Object { $_.Job })
        Wait-Job -Job $running -Any -Timeout 10 | Out-Null
    }

    $finishedAt = [DateTimeOffset]::Now
    $errors = @()
    foreach ($record in $records) {
        $job = $record.Job
        if ($job.State -eq 'Failed') {
            $errors += "match $($record.MatchId): $(Get-JobFailureText $job)"
        }
        if (!(Test-Path -LiteralPath $record.Report -PathType Leaf)) {
            $errors += "match $($record.MatchId): delay report missing"
        }
        $script:PROBE_ENTRIES.Add([ordered]@{
            phase = $Phase
            matchId = $record.MatchId
            startedAt = $record.StartedAt.ToString('o')
            finishedAt = $finishedAt.ToString('o')
            report = ('delay/' + $Phase + '/' + $record.MatchId + '.json')
            serials = @($record.Serials)
        })
    }
    Write-JsonFile (Join-Path $script:OutputRoot 'delay-probes.json') ([ordered]@{
        schemaVersion = 1
        probes = @($script:PROBE_ENTRIES)
    })
    if ($errors.Count -gt 0) { throw ("Delay probe failed: " + ($errors -join '; ')) }
    return [pscustomobject]@{ Phase = $Phase; StartedAt = $startedAt; FinishedAt = $finishedAt; Reports = $records.Count }
}

function Receive-MonitorOutputs {
    $statuses = @()
    foreach ($record in $script:MONITOR_RECORDS) {
        $job = $record.Job
        $targetDirectory = Join-Path $script:OutputRoot $record.Target.evidenceDirectory
        try {
            $output = @(Receive-Job -Job $job -ErrorAction SilentlyContinue)
            if ($output.Count -gt 0) {
                Write-Utf8Text (Join-Path $targetDirectory 'monitor-output.txt') (
                    (($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) + [Environment]::NewLine
                )
            }
        }
        catch {}
        $statuses += [ordered]@{
            targetId = $record.Target.id
            state = [string]$job.State
            error = (Get-JobFailureText $job)
        }
    }
    return $statuses
}

function Wait-ForMonitors {
    $deadline = [DateTimeOffset]::Now.AddSeconds(($script:CaptureMinutes * 60) + 900)
    while (@($script:MONITOR_RECORDS | Where-Object { $_.Job.State -eq 'Running' }).Count -gt 0) {
        if ([DateTimeOffset]::Now -gt $deadline) { throw 'Monitor collection timed out.' }
        $running = @($script:MONITOR_RECORDS | Where-Object { $_.Job.State -eq 'Running' } | ForEach-Object { $_.Job })
        Wait-Job -Job $running -Any -Timeout 30 | Out-Null
    }
    $failed = @($script:MONITOR_RECORDS | Where-Object { $_.Job.State -ne 'Completed' })
    if ($failed.Count -gt 0) {
        $details = @($failed | ForEach-Object { "$($_.Target.id): $($_.Job.State) $(Get-JobFailureText $_.Job)" })
        throw ("Monitor job failed: " + ($details -join '; '))
    }
}

function Capture-AdbText([string]$Serial, [string[]]$Arguments, [string]$Path) {
    $output = @(& $AdbPath -s $Serial @Arguments 2>&1)
    Write-Utf8Text $Path ((($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) + [Environment]::NewLine)
    return ($output -join [Environment]::NewLine)
}

function Capture-PostRunEvidence {
    foreach ($target in $matrix) {
        $directory = Join-Path $script:OutputRoot $target.evidenceDirectory
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
        Capture-AdbText $target.serial @('logcat','-b','crash','-d','-v','threadtime') (Join-Path $directory 'crash-buffer.txt')
        Capture-AdbText $target.serial @('logcat','-b','events','-d','-v','threadtime') (Join-Path $directory 'events.txt')
        Capture-AdbText $target.serial @('shell','dumpsys','activity','exit-info',$target.package) (Join-Path $directory 'exit-info.txt')
        $pidText = Capture-AdbText $target.serial @('shell','pidof',$target.package) (Join-Path $directory 'post-pid.txt')
        $target | Add-Member -NotePropertyName postPid -NotePropertyValue $pidText.Trim() -Force
    }
}

$script:OutputRoot = ''
$acceptanceError = $null
$exitCode = 1
$script:CaptureMinutes = $Minutes + [int][math]::Ceiling($MaxStartupSkewSeconds / 60.0) + 1

try {
    if (Test-Path -LiteralPath $OutputDirectory) { throw 'Use a new evidence directory for every release acceptance run.' }
    New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
    $script:OutputRoot = (Resolve-Path -LiteralPath $OutputDirectory).Path

    foreach ($target in $matrix) {
        $expectedHash = $candidates.mobile.apkSha256
        if ($target.variant -eq 'tv') { $expectedHash = $candidates.tv.apkSha256 }
        $target | Add-Member -NotePropertyName evidenceDirectory -NotePropertyValue ('runs/' + $target.id) -Force
        $target | Add-Member -NotePropertyName expectedApkSha256 -NotePropertyValue $expectedHash -Force
    }
    $Control['captureMinutes'] = $script:CaptureMinutes
    $matrixDocument = [ordered]@{
        schemaVersion = 1
        control = $Control
        candidates = $candidates
        targets = @($matrix)
    }
    Write-JsonFile (Join-Path $script:OutputRoot 'matrix.json') $matrixDocument
    Write-JsonFile (Join-Path $script:OutputRoot 'orchestration.json') ([ordered]@{
        status = 'running'
        startedAt = [DateTimeOffset]::Now.ToString('o')
        allTargetsReadyAt = $null
        completedAt = $null
        error = $null
        captureMinutes = $script:CaptureMinutes
        requiredContinuousSeconds = $Minutes * 60
    })

    foreach ($target in $matrix) {
        $deviceState = ((& $AdbPath -s $target.serial get-state 2>&1) -join ' ').Trim()
        if ($deviceState -ne 'device') { throw "Device unavailable: $($target.serial) ($deviceState)" }
    }

    foreach ($target in $matrix) {
        $targetDirectory = Join-Path $script:OutputRoot $target.evidenceDirectory
        New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
        $apkPath = if ($target.variant -eq 'tv') { $TvApk } else { $MobileApk }
        if ($InstallApks) {
            $installOutput = @(& $AdbPath -s $target.serial install -r -d $apkPath 2>&1)
            $installText = ($installOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
            Write-Utf8Text (Join-Path $targetDirectory 'install.txt') ($installText + [Environment]::NewLine)
            if ($LASTEXITCODE -ne 0 -or $installText -notmatch 'Success') {
                throw "APK install failed on $($target.serial): $installText"
            }
        }
        $installed = ((& $AdbPath -s $target.serial shell pm path $target.package 2>&1) -join ' ').Trim()
        if ($installed -notmatch '^package:') { throw "App package missing after install: $($target.serial)/$($target.package)" }
    }

    foreach ($target in $matrix) {
        foreach ($testPackage in @($TV_PACKAGE, $MOBILE_PACKAGE)) {
            & $AdbPath -s $target.serial shell am force-stop $testPackage 2>&1 | Out-Null
        }
        $clearOutput = @(& $AdbPath -s $target.serial logcat -b all -c 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to clear logcat on $($target.serial): $(($clearOutput -join ' ').Trim())"
        }
        $targetDirectory = Join-Path $script:OutputRoot $target.evidenceDirectory
        $launchOutput = @(& $AdbPath -s $target.serial shell am start -W -n "$($target.package)/com.qiubo.optimaltv.MainActivity" --es otvDebugMatchId $target.matchId 2>&1)
        $launchText = ($launchOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        Write-Utf8Text (Join-Path $targetDirectory 'launch.txt') ($launchText + [Environment]::NewLine)
        if ($LASTEXITCODE -ne 0 -or $launchText -match 'Error:|Exception|does not exist') {
            throw "Unable to launch $($target.package) on $($target.serial): $launchText"
        }
        # Start log capture immediately after each launch. Starting all monitors
        # after the last launch would miss early first-frame logs on fast devices.
        Start-MonitorJob $target
    }

    $readyDeadline = [DateTimeOffset]::Now.AddSeconds($LaunchTimeoutSeconds)
    $firstReadyAt = $null
    while ($true) {
        foreach ($record in $script:MONITOR_RECORDS) {
            if ($record.Job.State -in @('Completed','Failed','Stopped')) {
                throw "Monitor ended before first frame: $($record.Target.id) $($record.Job.State) $(Get-JobFailureText $record.Job)"
            }
        }
        $readyTargets = @($matrix | Where-Object { Test-TargetReady $_ })
        if (!$firstReadyAt -and $readyTargets.Count -gt 0) { $firstReadyAt = [DateTimeOffset]::Now }
        if ($readyTargets.Count -eq $matrix.Count) { break }
        if ([DateTimeOffset]::Now -gt $readyDeadline) {
            $waiting = @($matrix | Where-Object { $_.id -notin $readyTargets.id } | ForEach-Object { $_.id })
            throw ("First-frame timeout; waiting: " + ($waiting -join ', '))
        }
        Start-Sleep -Seconds 2
    }
    $allReadyAt = [DateTimeOffset]::Now
    if (($allReadyAt - $firstReadyAt).TotalSeconds -gt $MaxStartupSkewSeconds) {
        throw "First-frame startup skew exceeded $MaxStartupSkewSeconds seconds."
    }
    Update-Orchestration @{ allTargetsReadyAt = $allReadyAt.ToString('o'); startupSkewSeconds = [math]::Round(($allReadyAt - $firstReadyAt).TotalSeconds, 3) }

    $initialProbe = Invoke-DelayProbePhase 'initial'
    Update-Orchestration @{ initialDelayFinishedAt = $initialProbe.FinishedAt.ToString('o') }
    if ((Invoke-Evaluator 'early' 'summary-early.json') -ne 0) {
        throw 'Early live health gate failed; stopping the long run immediately.'
    }
    if ((Invoke-Evaluator 'initial' 'summary-initial-delay.json') -ne 0) {
        throw 'Initial <=5s delay gate failed; stopping the long run immediately.'
    }

    $finalProbeAt = $allReadyAt.AddSeconds(($Minutes * 60) + 2)
    while ([DateTimeOffset]::Now -lt $finalProbeAt) {
        $ended = @($script:MONITOR_RECORDS | Where-Object { $_.Job.State -ne 'Running' })
        if ($ended.Count -gt 0) {
            $details = @($ended | ForEach-Object { "$($_.Target.id): $($_.Job.State) $(Get-JobFailureText $_.Job)" })
            throw ("Monitor ended before the final delay window: " + ($details -join '; '))
        }
        $remaining = [math]::Max(1, [math]::Min(30, [int][math]::Ceiling(($finalProbeAt - [DateTimeOffset]::Now).TotalSeconds)))
        Start-Sleep -Seconds $remaining
    }

    $finalProbe = Invoke-DelayProbePhase 'final'
    Update-Orchestration @{ finalDelayFinishedAt = $finalProbe.FinishedAt.ToString('o') }
    if ((Invoke-Evaluator 'final' 'summary-final-delay.json') -ne 0) {
        throw 'Final <=5s/no-accumulation delay gate failed.'
    }

    Wait-ForMonitors
    $monitorStatuses = Receive-MonitorOutputs
    Capture-PostRunEvidence
    $completedAt = [DateTimeOffset]::Now
    Update-Orchestration @{
        status = 'capture-completed'
        completedAt = $completedAt.ToString('o')
        monitorJobs = @($monitorStatuses)
        error = $null
    }

    $exitCode = Invoke-Evaluator 'all' 'summary.json'
    if ($exitCode -eq 0) {
        Write-Output ('PASS: ' + (Join-Path $script:OutputRoot 'summary.json'))
    }
    else {
        Write-Output ('FAIL: ' + (Join-Path $script:OutputRoot 'summary.json'))
    }
}
catch {
    $acceptanceError = $_.Exception.Message
    [Console]::Error.WriteLine("Acceptance aborted: $acceptanceError")
    if ($script:OutputRoot -and (Test-Path -LiteralPath $script:OutputRoot)) {
        try {
            Stop-TrackedJobs
            $statuses = @(Receive-MonitorOutputs)
            Update-Orchestration @{
                status = 'capture-failed'
                completedAt = [DateTimeOffset]::Now.ToString('o')
                error = $acceptanceError
                monitorJobs = $statuses
            }
            $null = Invoke-Evaluator 'all' 'summary.json'
        }
        catch {
            [Console]::Error.WriteLine("Unable to finalize failure evidence: $($_.Exception.Message)")
        }
    }
    $exitCode = 1
}
finally {
    Stop-TrackedJobs
    foreach ($record in $ACTIVE_JOBS) {
        try { Remove-Job -Job $record.Job -Force -ErrorAction SilentlyContinue } catch {}
    }
}

exit $exitCode
