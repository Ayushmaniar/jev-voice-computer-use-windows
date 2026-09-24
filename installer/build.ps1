<#
Builds dist\JevVoiceSetup-<version>.exe: a per-user Windows installer that needs no Python on the target PC.

It bundles a relocatable CPython (python-build-standalone) with the app's packages already installed, the
app code, and a small launcher with the app icon. The optional NVIDIA speech libraries (about 1.3 GB) are not
bundled: Setup downloads the pinned wheels, checks their SHA-256, and installs them only if the user wants them.

Needs Inno Setup 6.5+ (winget install JRSoftware.InnoSetup) and internet access. Run from anywhere:
    .\installer\build.ps1 [-Version 1.0.0] [-Clean]
#>
param(
    [string]$Version = '1.0.0',
    [switch]$Clean
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$here = $PSScriptRoot
$root = (Resolve-Path (Join-Path $here '..')).Path
$build = Join-Path $here 'build'
$stage = Join-Path $build 'stage'
$cache = Join-Path $build 'cache'
$runtime = Join-Path $stage 'runtime'
$python = Join-Path $runtime 'python.exe'

# Pinned relocatable CPython with Tk (the app's UI) and pip.
$pythonUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz'
$pythonSha256 = 'e90c1b6419da3bd812dd73bb3de40287a21abf153438147639ec5e20375ea93f'

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Invoke-Checked([string]$exe, [string[]]$arguments) {
    # The bundled Python must ignore this machine's PYTHON* variables and user site-packages, as it does when installed.
    if ($exe -eq $python) { $arguments = @('-E', '-s') + $arguments }
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($exe)) failed with exit code $LASTEXITCODE" }
}

$iscc = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 is not installed. Install it with: winget install JRSoftware.InnoSetup' }
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

if ($Clean -and (Test-Path $build)) { Remove-Item -Recurse -Force $build }
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage, $cache | Out-Null

Step 'Python runtime'
$archive = Join-Path $cache 'python.tar.gz'
if (-not (Test-Path $archive) -or (Get-FileHash $archive -Algorithm SHA256).Hash -ne $pythonSha256) {
    Invoke-WebRequest -Uri $pythonUrl -OutFile $archive
    if ((Get-FileHash $archive -Algorithm SHA256).Hash -ne $pythonSha256) { throw 'Python runtime checksum mismatch' }
}
$extract = Join-Path $build 'extract'
if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
New-Item -ItemType Directory -Force -Path $extract | Out-Null
Invoke-Checked "$env:WINDIR\System32\tar.exe" @('-xzf', $archive, '-C', $extract)
Move-Item (Join-Path $extract 'python') $runtime
Remove-Item -Recurse -Force $extract

# Windows ships these C++ runtime DLLs only with the Visual C++ Redistributable; ship them app-locally so a
# clean PC needs nothing else. Python's own vcruntime140.dll is already in the runtime.
foreach ($dll in 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'concrt140.dll') {
    $source = Join-Path $env:WINDIR "System32\$dll"
    if (-not (Test-Path (Join-Path $runtime $dll)) -and (Test-Path $source)) { Copy-Item $source $runtime }
}

Step 'App packages'
$requirements = Join-Path $build 'requirements-app.txt'
Get-Content (Join-Path $root 'requirements-voice.txt') | Where-Object { $_ -notmatch '^\s*nvidia-' } | Set-Content $requirements
Invoke-Checked $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-warn-script-location',
                         '--only-binary=:all:', '-r', $requirements)
# Console-script shims hold this build machine's paths; the app never uses them.
Remove-Item -Recurse -Force (Join-Path $runtime 'Scripts') -ErrorAction SilentlyContinue
# Put {app} on sys.path, so `python -m voice_control.configure` works from any folder, and {app}\gpu, where
# Setup installs the optional NVIDIA libraries (outside runtime\, so they survive upgrades).
Set-Content -Encoding ascii (Join-Path $runtime 'Lib\site-packages\jev.pth') @('../../..', '../../../gpu')

Step 'App code'
$exclude = @('test_*.py', 'bench_*', 'goal_eval.py', 'goal_inspect.py', 'goal_probe*', 'goal_replay.py', 'goal_sim*',
             'goal_tasks.json', 'replay.py', 'set_shortcut_appid.py', '*.md', '*.ps1', '*.cs', 'jev-voice-concept.png')
& robocopy (Join-Path $root 'voice_control') (Join-Path $stage 'voice_control') /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ /XF @exclude | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
Push-Location $stage
try {
    # Fails the build if the app imports something that was left out.
    Invoke-Checked $python @('-c', 'import voice_control.app')
} finally { Pop-Location }
foreach ($unused in 'Lib\idlelib', 'Lib\turtledemo') { Remove-Item -Recurse -Force (Join-Path $runtime $unused) -ErrorAction SilentlyContinue }
# Byte-compile up front so the first start isn't slowed down (and the files don't need to be writable).
Invoke-Checked $python @('-m', 'compileall', '-q', '-j', '0', (Join-Path $runtime 'Lib'), (Join-Path $stage 'voice_control'))

# configure.cmd: the terminal setup tool (find-keys, set-key, status) with the bundled Python.
Set-Content -Encoding ascii (Join-Path $stage 'configure.cmd') @(
    '@echo off',
    'rem Jev Voice setup from a terminal. Run "configure.cmd --help" for the commands.',
    '"%~dp0runtime\python.exe" -E -s -P -m voice_control.configure %*'
)

Step 'Launcher'
$icon = Join-Path $root 'voice_control\assets\jev-voice-logo.ico'
Invoke-Checked $csc @('/nologo', '/target:winexe', '/r:System.Windows.Forms.dll', "/win32icon:$icon",
                      "/out:$(Join-Path $stage 'JevVoice.exe')", (Join-Path $root 'voice_control\launcher.cs'))

Step 'Artwork'
Invoke-Checked $python @((Join-Path $here 'make_art.py'), (Join-Path $build 'art'))

Step 'GPU downloads'
# Resolve the newest NVIDIA wheels allowed by requirements-voice.txt and pin their URLs and hashes into Setup.
$gpuSpecs = Get-Content (Join-Path $root 'requirements-voice.txt') | Where-Object { $_ -match '^\s*nvidia-' } | ForEach-Object { $_.Trim() }
$report = Join-Path $build 'gpu-report.json'
Invoke-Checked $python (@('-m', 'pip', 'install', '--disable-pip-version-check', '--dry-run', '--ignore-installed',
                          '--only-binary=:all:', '--quiet', '--report', $report) + $gpuSpecs)
$wheels = (Get-Content $report -Raw | ConvertFrom-Json).install | ForEach-Object {
    $url = $_.download_info.url
    $name = [Uri]::UnescapeDataString(($url -split '/')[-1])
    $size = [long]@((Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing).Headers['Content-Length'])[0]
    [pscustomobject]@{ Url = $url; Name = $name; Sha256 = $_.download_info.archive_info.hashes.sha256; Size = $size
                       DistInfo = (($name -split '-')[0..1] -join '-') + '.dist-info' }
}
if (-not $wheels) { throw 'Could not resolve the NVIDIA wheels' }
$gpuSize = '{0:N1} GB' -f (($wheels | Measure-Object Size -Sum).Sum / 1GB)
# Inno's Parameters strings double their quotes: ""{tmp}\a.whl"" ""{tmp}\b.whl""
$wheelArgs = ($wheels | ForEach-Object { '""{tmp}\' + $_.Name + '""' }) -join ' '
$lines = @(
    "; Generated by build.ps1: the NVIDIA wheels Setup downloads for the GPU task.",
    "#define GpuSize `"$gpuSize`"",
    "#define GpuWheelArgs '$wheelArgs'",
    '',
    '[Code]',
    'procedure AddGpuDownloads(Page: TDownloadWizardPage);',
    'begin'
) + ($wheels | ForEach-Object { "  Page.Add('$($_.Url)', '$($_.Name)', '$($_.Sha256)');" }) + @(
    'end;',
    '',
    'function GpuAlreadyInstalled: Boolean;',
    'begin',
    "  Result := $(($wheels | ForEach-Object { "DirExists(ExpandConstant('{app}\gpu\$($_.DistInfo)'))" }) -join ' and ');",
    'end;'
)
Set-Content -Encoding utf8 (Join-Path $build 'gpu.iss') $lines
$wheels | ForEach-Object { Write-Host ("    {0} ({1:N0} MB)" -f $_.Name, ($_.Size / 1MB)) }

Step 'Installer'
Invoke-Checked $iscc @('/Q', "/DAppVersion=$Version", (Join-Path $here 'JevVoice.iss'))
$setup = Join-Path $here "dist\JevVoiceSetup-$Version.exe"
Write-Host ("Built {0} ({1:N0} MB)" -f $setup, ((Get-Item $setup).Length / 1MB)) -ForegroundColor Green
