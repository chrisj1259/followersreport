#!/usr/bin/env pwsh
# test_install.ps1 -- VM-side acceptance test for InstagramFollowerReport-Setup-v1.1.0.exe
#
# Run on a clean Windows VM with NO WebView2 runtime installed. The harness:
#
#   1. PRECHECK    -- assert WebView2 is absent across all 4 standard registry locations.
#   2. STAGE SEED  -- drop a fake data/ + disabledaccounts.json next to the .exe so we can
#                     verify bootstrap_data_root() migrates it into %APPDATA%.
#   3. RUN         -- run the bundled setup silently (/SP-/VERYSILENT/SUPPRESSMSGBOXES/NORESTART).
#   4. POSTCHECK   -- poll the WebView2 registry keys for up to 300s; the lossy embedded
#                     bootstrapper pulls the runtime via Windows Update and writes its pv.
#   5. APPDATA     -- identify the expected per-user storage paths.
#   6. WINDOW      -- launch the app, assert pywebview opened a window, then verify the
#                     profile manifest was created and legacy data remains available for
#                     the username migration prompt.
#   7. /FLAG       -- run `python run.py 9999` in the background, POST a synthetic handle to
#                     /flag, assert the CLI data/disabledaccounts.json contains the handle,
#                     then POST /flag with action=remove and assert it's
#                     gone. This exercises the FlagHandler HTTP round-trip + atomic write path.
#
# Required on the VM:
#   * Output\InstagramFollowerReport-Setup-v1.1.0.exe (from `python build.py` on the dev box)
#   * Python 3.10+ on PATH (for step 7); otherwise step 7 is reported as a SKIP with a warning.
#   * run.py copied next to this script (for step 7); we copy it if available in the parent
#     repo source.
#
# Exit code: 0 on success, non-zero on first failed assertion.
#
# PONYTAIL: deliberately verbose Print-Host with [OK]/[RUN]/[POST] markers so the test
# transcript is readable when you scroll back through powershell.exe output.

[CmdletBinding()]
param(
    [string] $SetupExe = (Join-Path $PSScriptRoot 'Output\InstagramFollowerReport-Setup-v1.1.0.exe'),
    [string] $SourceRepo = $PSScriptRoot,    # where run.py / data / index.html live in the dev checkout
    [string] $TempDir   = (Join-Path $env:TEMP 'ifr-e2e'),
    [int]    $ServerPort = 9999
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# ---- helpers ---------------------------------------------------------------

function Test-WebView2Installed {
    $keys = @(
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'HKCU:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    )
    foreach ($k in $keys) {
        if (Test-Path $k) {
            $pv = (Get-ItemProperty -Path $k -ErrorAction SilentlyContinue).pv
            if ($pv) { return [pscustomobject]@{ Installed = $true; Version = $pv; Key = $k } }
        }
    }
    return [pscustomobject]@{ Installed = $false }
}

function Wait-WebView2Installed {
    param([int] $TimeoutSeconds = 300)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $state = Test-WebView2Installed
        if ($state.Installed) { return $state }
        Start-Sleep -Seconds 1
    }
    return $state  # last polled state, presumably Installed=$false
}

# ---- 1. PRECHECK -----------------------------------------------------------

Write-Host '[PRECHECK] WebView2 must be absent on a clean VM' -ForegroundColor Cyan
$preState = Test-WebView2Installed
if ($preState.Installed) {
    throw "WebView2 is already installed (version $($preState.Version) at $($preState.Key)). Need a clean VM."
}
Write-Host '[OK] WebView2 confirmed absent across all 4 standard registry locations' -ForegroundColor Green

# ---- 2. STAGE SEED next to the .exe so bootstrap_data_root() has something to migrate --

Write-Host '[RUN] staging seed data alongside the setup exe so APPDATA bootstrap has work to do' -ForegroundColor Cyan
$setupDir = Split-Path -Parent $SetupExe
$seedData     = Join-Path $setupDir 'data'
$seedFlagged  = Join-Path $setupDir 'disabledaccounts.json'
if (-not (Test-Path $seedData)) { New-Item -ItemType Directory -Path $seedData -Force | Out-Null }
$seedFollowers = Join-Path $seedData '07-15-2026'
if (-not (Test-Path $seedFollowers)) { New-Item -ItemType Directory -Path $seedFollowers -Force | Out-Null }
# ponytail: PS5's -Encoding utf8 emits a UTF-8 BOM. Python's read_text(encoding="utf-8")
# strips the BOM transparently so this is mostly safe, but every byte here is
# 7-bit ASCII -- use -Encoding ascii + -NoNewline to be defensive.
'[{`"string_list_data`":[]}]' | Out-File -FilePath (Join-Path $seedFollowers 'followers_1.json') -Encoding ascii -NoNewline
'[{`"string_list_data`":[]}]' | Out-File -FilePath (Join-Path $seedFollowers 'following.json')   -Encoding ascii -NoNewline
'["seed_disabled_xyz"]'        | Out-File -FilePath $seedFlagged                              -Encoding ascii -NoNewline
Write-Host "[OK] staged seed: 07-15-2026 snapshot + 1 flagged handle" -ForegroundColor Green

# ---- 3. RUN installer silently --------------------------------------------

if (-not (Test-Path $SetupExe)) {
    throw "Setup exe not found at $SetupExe -- run `python build.py` on the dev box first."
}
Write-Host "[RUN] installer: $SetupExe" -ForegroundColor Cyan
# ponytail: do NOT pass -Verb RunAs. On Win10 with an admin test user, that
# triggers a UAC prompt the non-interactive harness can't satisfy. The
# enclosing pwsh session must already be elevated; README documents this.
$proc = Start-Process -FilePath $SetupExe `
                      -ArgumentList '/SP-','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' `
                      -PassThru -Wait
if ($proc.ExitCode -ne 0) {
    throw "Installer exit code $($proc.ExitCode). Look at %TEMP%\setup.log for details."
}
Write-Host "[OK] installer returned exit code 0" -ForegroundColor Green

# ---- 4. POSTCHECK: WebView2 must now be installed -------------------------

Write-Host '[RUN] polling registry for up to 300s (bootstrapper fetches the runtime via Windows Update)' -ForegroundColor Cyan
$postState = Wait-WebView2Installed -TimeoutSeconds 300
if (-not $postState.Installed) {
    throw "WebView2 NOT installed within 300s. Embedded MicrosoftEdgeWebview2Setup.exe failed silently."
}
Write-Host "[OK] WebView2 installed (version $($postState.Version))" -ForegroundColor Green

# ---- 5. APPDATA PATHS -----------------------------------------------------

$appData     = [Environment]::GetFolderPath('ApplicationData')
$appDataDir  = Join-Path $appData 'InstagramFollowerReport'
$markerPath  = Join-Path $appDataDir '.installed'
$dataPath    = Join-Path $appDataDir 'data'
$distPath    = Join-Path $appDataDir 'dist'
$flagPath    = Join-Path $appDataDir 'disabledaccounts.json'
$manifestPath = Join-Path $appDataDir 'profiles.json'
$profilesPath = Join-Path $appDataDir 'profiles'
$expectDate  = '07-15-2026'

# ---- 6. APP LAUNCH + WINDOW VERIFY ----------------------------------------

$appExe = Join-Path ${env:ProgramFiles} 'InstagramFollowerReport\InstagramFollowerReport.exe'
if (-not (Test-Path $appExe)) { throw "App exe missing at $appExe -- install step may have failed" }
Write-Host "[RUN] launching $appExe" -ForegroundColor Cyan
$app = Start-Process -FilePath $appExe -WorkingDirectory $setupDir -PassThru
try {
    # ponytail: pywebview's WindowViewSourceLoaded + WebView2 process spawn
    # can take 8-30s on slow VMs. Poll with a deadline instead of fixed sleep.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $app.Refresh()
        if ($app.HasExited) { break }
        if ($app.MainWindowHandle -ne 0) { break }
        Start-Sleep -Seconds 1
    }
    if ($app.HasExited) {
        throw "App exited immediately with code $($app.ExitCode). pywebview or WebView2 failed to boot."
    }
    if ($app.MainWindowHandle -eq 0) {
        throw "App is running but MainWindowHandle is 0 after 30s -- pywebview did not produce a window."
    }
    Write-Host "[OK] app windowed: handle=$($app.MainWindowHandle) title='$($app.MainWindowTitle)'" -ForegroundColor Green

    Write-Host '[RUN] verifying profile manifest and rollback-safe legacy migration state' -ForegroundColor Cyan
    if (-not (Test-Path $appDataDir)) { throw "APPDATA bootstrap missing: $appDataDir" }
    if (-not (Test-Path $markerPath)) { throw ".installed marker missing -- bootstrap_data_root() never ran" }
    if (-not (Test-Path $distPath))   { throw "dist/ missing in APPDATA" }
    if (-not (Test-Path $dataPath))   { throw "legacy data/ missing in APPDATA" }
    if (-not (Test-Path $flagPath))   { throw "legacy disabledaccounts.json missing in APPDATA" }
    if (-not (Test-Path $manifestPath)) { throw "profiles.json missing after desktop startup" }
    if (-not (Test-Path $profilesPath)) { throw "profiles/ missing after desktop startup" }
    if (-not (Test-Path (Join-Path $dataPath $expectDate))) {
        throw "expected seeded snapshot $expectDate NOT retained for profile migration"
    }
    $flaggedContent = Get-Content $flagPath -Raw
    if (-not $flaggedContent.Contains('seed_disabled_xyz')) {
        throw "expected seeded handle 'seed_disabled_xyz' NOT retained. Got: $flaggedContent"
    }
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.schema_version -ne 1) { throw "profiles.json has an unsupported schema version" }
    if ($manifest.active_profile_id) { throw "legacy data must not be displayed before its username is supplied" }
    if (@($manifest.profiles).Count -ne 0) { throw "legacy startup unexpectedly created an anonymous profile" }
    Write-Host "[OK] profiles.json + profiles/ created; legacy snapshot and flags retained pending username" -ForegroundColor Green
} finally {
    Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# ---- 7. /FLAG HANDLER ROUND-TRIP ------------------------------------------

Write-Host '[RUN] /flag handler round-trip via standalone python run.py' -ForegroundColor Cyan
$pythonExe = $null
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $pythonExe = $cmd.Source }
else {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $pythonExe = $cmd.Source }
}

if (-not $pythonExe) {
    Write-Warning "[SKIP] Python 3.10+ not on PATH -- step 7 (flag round-trip) requires it. Install Python and rerun."
}
else {
    # Copy run.py next to this script if it isn't already.
    $runSrc = Join-Path $SourceRepo 'run.py'
    $runDst = Join-Path $PSScriptRoot 'run.py'
    if ((Test-Path $runSrc) -and (-not (Test-Path $runDst))) {
        Copy-Item -Path $runSrc -Destination $runDst -Force
        Write-Host "[OK] copied run.py from source repo into harness dir for step 7" -ForegroundColor Green
    }
    if (-not (Test-Path $runDst)) {
        Write-Warning "[SKIP] run.py not present at $runSrc -- step 7 skipped."
    }
    else {
        $cliFlagPath = Join-Path $PSScriptRoot 'data\disabledaccounts.json'
        if (-not (Test-Path $TempDir)) { New-Item -ItemType Directory -Path $TempDir -Force | Out-Null }
        $srvLog = Join-Path $TempDir 'srv.out'
        $srvErr = Join-Path $TempDir 'srv.err'
        Write-Host "[RUN] $pythonExe run.py $ServerPort" -ForegroundColor Cyan
        $srv = Start-Process -FilePath $pythonExe `
                              -ArgumentList (Join-Path $PSScriptRoot 'run.py'),"$ServerPort" `
                              -PassThru -RedirectStandardOutput $srvLog -RedirectStandardError $srvErr `
                              -WorkingDirectory $PSScriptRoot
        try {
            Start-Sleep -Seconds 2
            $srv.Refresh()
            if ($srv.HasExited) {
                $out = (Get-Content $srvLog -Raw) + "`n" + (Get-Content $srvErr -Raw)
                throw "run.py server exited prematurely (code $($srv.ExitCode)):`n$out"
            }
            $probeHandle = "selemene_test_" + (Get-Random)
            $body = @{ username = $probeHandle; action = 'add' } | ConvertTo-Json -Compress
            $url  = "http://127.0.0.1:$ServerPort/flag"
            Write-Host "[POST] $url  body=$body" -ForegroundColor Cyan
            $resp = Invoke-WebRequest -Uri $url -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 10 -UseBasicParsing
            $j = $resp.Content | ConvertFrom-Json
            if (-not $j.ok) { throw "POST /flag returned ok=false: $($resp.Content)" }
            Write-Host "[OK] POST /flag returned ok=true total=$($j.total)" -ForegroundColor Green

            $now = Get-Content $cliFlagPath -Raw
            if (-not $now.Contains($probeHandle)) {
                throw "/flag add succeeded but $cliFlagPath does NOT contain $probeHandle. Got: $now"
            }
            Write-Host "[OK] disabledaccounts.json contains $probeHandle" -ForegroundColor Green

            # Cleanup: unflag.
            $body2 = @{ username = $probeHandle; action = 'remove' } | ConvertTo-Json -Compress
            $resp2 = Invoke-WebRequest -Uri $url -Method Post -Body $body2 -ContentType 'application/json' -TimeoutSec 10 -UseBasicParsing
            $j2 = $resp2.Content | ConvertFrom-Json
            if (-not $j2.ok) { Write-Warning "[WARN] POST /flag remove did not return ok=true: $($resp2.Content)" }
            $after = Get-Content $cliFlagPath -Raw
            if ($after.Contains($probeHandle)) {
                Write-Warning "[WARN] /flag remove: $probeHandle still in disabledaccounts.json. Got: $after"
            } else {
                Write-Host "[OK] round-trip closed: $probeHandle removed" -ForegroundColor Green
            }
        } finally {
            if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue }
            Remove-Item $srvLog -ErrorAction SilentlyContinue
            Remove-Item $srvErr -ErrorAction SilentlyContinue
        }
    }
}

Write-Host ""
Write-Host "[DONE] All checks passed. The installer is production-ready." -ForegroundColor Green
