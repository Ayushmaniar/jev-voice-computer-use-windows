<#
One-command, non-interactive install of Jev Voice Control, for people and coding agents.

    .\installer\install.ps1                          # build Setup if needed, install, find a key, start the app
    .\installer\install.ps1 -KeyFile C:\proj\.env    # use the key in that file (OPENROUTER_API_KEY=... etc.)
    .\installer\install.ps1 -NoGpu -NoStartup        # skip the ~1.3 GB NVIDIA download / the sign-in start

It runs JevVoiceSetup.exe silently (per user, no admin prompt), then prints the install status. Without -Key or
-KeyFile it keeps a key saved earlier, else uses the first key found in the environment or in .env files in the
usual project folders; `configure.cmd find-keys` in the install folder lists them and `set-key` changes it.
Exit code: 0 ready to use; 2 installed but no API key yet; anything else failed (see the log path it prints).
#>
param(
    [string]$Setup,          # an existing JevVoiceSetup-*.exe; default: the newest in installer\dist, built if missing
    [string]$KeyFile,        # a .env file (or a file holding just the key)
    [string]$Key,            # the key itself; prefer -KeyFile, which keeps it out of process listings
    [switch]$NoGpu,
    [switch]$NoStartup,
    [switch]$NoDesktop,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

if (-not $Setup) {
    $Setup = Get-ChildItem (Join-Path $here 'dist') -Filter 'JevVoiceSetup-*.exe' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $Setup) {
        Write-Host 'No installer built yet; building it (a few minutes, needs Inno Setup and internet).'
        & (Join-Path $here 'build.ps1')
        $Setup = Get-ChildItem (Join-Path $here 'dist') -Filter 'JevVoiceSetup-*.exe' |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
}
$Setup = (Resolve-Path $Setup).Path

$tasks = @()
if (-not $NoDesktop) { $tasks += 'desktopicon' }
if (-not $NoStartup) { $tasks += 'startup' }
if (-not $NoGpu) { $tasks += 'gpu' }   # ignored by Setup on PCs without an NVIDIA driver
$log = Join-Path $env:TEMP 'JevVoiceSetup.log'
$arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$log`"", "/TASKS=`"$($tasks -join ',')`"")

$tempKey = $null
if ($Key) {
    $tempKey = Join-Path $env:TEMP "jev-key-$([guid]::NewGuid()).txt"
    Set-Content -LiteralPath $tempKey -Value $Key.Trim() -Encoding ascii
    $KeyFile = $tempKey
}
if ($KeyFile) { $arguments += "/KEYFILE=`"$((Resolve-Path $KeyFile).Path)`"" }

Write-Host "Installing with $([IO.Path]::GetFileName($Setup))$(if ($tasks -contains 'gpu') { ' (the GPU download can take several minutes)' })..."
try {
    $process = Start-Process -FilePath $Setup -ArgumentList $arguments -Wait -PassThru
} finally {
    if ($tempKey) { Remove-Item -LiteralPath $tempKey -ErrorAction SilentlyContinue }
}
if ($process.ExitCode -ne 0) {
    Write-Error "Setup failed with exit code $($process.ExitCode). Log: $log"
    exit $process.ExitCode
}

$app = Join-Path $env:LOCALAPPDATA 'Programs\Jev Voice Control'
& (Join-Path $app 'configure.cmd') status
$ready = $LASTEXITCODE
if (-not $NoLaunch) {
    Start-Process (Join-Path $app 'JevVoice.exe')
    Write-Host 'Started Jev Voice: look for the pill at the bottom of the screen. Hold Right Ctrl and speak.'
}
if ($ready -eq 2) {
    Write-Host "No API key yet. Add one with: & `"$app\configure.cmd`" set-key --from-file <path to .env>"
}
Write-Host "Setup log: $log"
exit $ready
