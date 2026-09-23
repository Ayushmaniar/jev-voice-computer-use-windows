$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv-voice\Scripts\pythonw.exe'
$pythonConsole = Join-Path $root '.venv-voice\Scripts\python.exe'
$icon = Join-Path $PSScriptRoot 'assets\jev-voice-logo.ico'
$installDir = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Jev Voice Control'
$launcher = Join-Path $installDir 'JevVoiceLauncher.exe'
$source = Join-Path $PSScriptRoot 'launcher.cs'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

foreach ($path in @($python, $pythonConsole, $icon, $source, $compiler)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required file is missing: $path"
    }
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
& $compiler /nologo /target:winexe /r:System.Windows.Forms.dll "/win32icon:$icon" "/out:$launcher" $source
if ($LASTEXITCODE -ne 0) {
    throw "Launcher build failed with exit code $LASTEXITCODE"
}

$programs = [Environment]::GetFolderPath('Programs')
$shortcutPath = Join-Path $programs 'Jev Voice Control.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.Arguments = '"' + $root + '"'
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$launcher,0"
$shortcut.Description = 'Push-to-talk control for Windows apps'
$shortcut.Save()
& $pythonConsole (Join-Path $PSScriptRoot 'set_shortcut_appid.py') $shortcutPath 'Jev.VoiceControl'
if ($LASTEXITCODE -ne 0) {
    throw "Shortcut AppUserModelID registration failed with exit code $LASTEXITCODE"
}

Write-Output "Installed Start menu shortcut: $shortcutPath"
if (-not (Get-StartApps | Where-Object { $_.Name -eq 'Jev Voice Control' })) {
    Write-Warning 'Windows has not registered Jev Voice Control in Start apps yet. The shortcut exists, but Start search may not show it.'
}
