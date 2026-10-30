param(
    [Parameter(Mandatory=$true)][string]$Adb,
    # One independent Android target per entry: serial|package|matchId.
    [Parameter(Mandatory=$true)][string[]]$Targets,
    [int]$Minutes = 10,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$matrix = @($Targets | ForEach-Object {
    $parts = $_.Split('|')
    if ($parts.Count -ne 3) { throw 'Expected serial|package|matchId.' }
    if ($parts[1] -notin @('com.qiubo.optimaltv.public','com.qiubo.optimaltv.mobile.public')) { throw 'Unknown app package.' }
    if ($parts[2] -notmatch '^\d+$') { throw 'Expected a numeric match ID.' }
    [pscustomobject]@{ serial=$parts[0]; package=$parts[1]; matchId=$parts[2] }
})
$uniqueMatchCount = @($matrix.matchId | Select-Object -Unique).Count
if ($uniqueMatchCount -lt 3) { throw 'Release acceptance requires at least three different matches.' }
if (@($matrix.serial | Select-Object -Unique).Count -ne $matrix.Count) { throw 'Every match requires an independent device.' }
if ($Minutes -lt 10) { throw 'Acceptance duration must be at least 10 minutes.' }
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Use a new evidence directory.' }
foreach ($entry in $matrix) {
    if ((& $Adb -s $entry.serial get-state) -ne 'device') { throw "Device unavailable: $($entry.serial)" }
    if (!((& $Adb -s $entry.serial shell pm path $entry.package) -match '^package:')) { throw "App missing: $($entry.serial)" }
}
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$matrix | ConvertTo-Json | Out-File (Join-Path $OutputDirectory 'matrix.json') -Encoding utf8
$monitorScript = Join-Path $PSScriptRoot 'monitor_android_live.ps1'
$expectedBackendHash = (Get-FileHash (Join-Path $PSScriptRoot '../backend-src/proxy.py') -Algorithm SHA256).Hash
$jobs = @()
try {
    foreach ($entry in $matrix) {
        foreach ($testPackage in @('com.qiubo.optimaltv.public','com.qiubo.optimaltv.mobile.public')) {
            & $Adb -s $entry.serial shell am force-stop $testPackage | Out-Null
        }
        & $Adb -s $entry.serial logcat -c
        & $Adb -s $entry.serial shell am start -n "$($entry.package)/com.qiubo.optimaltv.MainActivity" --es otvDebugMatchId $entry.matchId | Out-Null
        $targetDirectory = Join-Path $OutputDirectory "$($entry.serial)-$($entry.matchId)"
        $jobs += Start-Job -ArgumentList $monitorScript,$Adb,$entry.serial,$entry.package,$entry.matchId,$Minutes,$targetDirectory,$expectedBackendHash -ScriptBlock {
            param($scriptPath,$adbPath,$serial,$packageName,$matchId,$duration,$directory,$backendHash)
            & $scriptPath -Adb $adbPath -Serial $serial -Package $packageName -MatchId $matchId -ExpectedBackendHash $backendHash -Minutes $duration -OutputDirectory $directory
        }
    }
    while (@($jobs | Where-Object State -eq 'Running').Count -gt 0) {
        $jobs | Receive-Job
        $jobs | Where-Object State -eq 'Running' | Wait-Job -Any -Timeout 30 | Out-Null
        if (@($jobs | Where-Object State -eq 'Failed').Count) { throw 'A collector failed; inspect its output.' }
    }
    $jobs | Receive-Job
} finally {
    $jobs | Where-Object State -eq 'Running' | Stop-Job
    $jobs | Remove-Job
}
$runs = @(Get-ChildItem -LiteralPath $OutputDirectory -Directory | ForEach-Object {
    $resultPath = Join-Path $_.FullName 'result.json'
    if (!(Test-Path -LiteralPath $resultPath)) { throw "Missing result: $resultPath" }
    Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
})
$summary = [ordered]@{
    uniqueMatches = @($runs.matchId | Select-Object -Unique).Count
    runs = $runs.Count
    passedRuns = @($runs | Where-Object pass).Count
    allPassed = (@($runs | Where-Object { -not $_.pass }).Count -eq 0) -and $uniqueMatchCount -ge 3
    results = $runs
}
$summary | ConvertTo-Json -Depth 5 | Out-File (Join-Path $OutputDirectory 'summary.json') -Encoding utf8
# Review actual match identity, source picture, frame progress and <=5s added delay separately.
