#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall')]
    [string]$Action = 'install',
    [switch]$Xedoc,
    [switch]$Claude,
    [switch]$Codex,
    [switch]$All,
    [string]$Source = $env:E47_MARKETPLACE_SOURCE,
    [switch]$WithContextRuntime
)

$ErrorActionPreference = 'Stop'

if (@($Xedoc, $Claude, $Codex, $All).Where({ $_ }).Count -gt 1) {
    throw 'Select only one of -Xedoc, -Claude, -Codex, or -All.'
}

# Do not pick up the WSL bash.exe from System32.
$candidates = @()
$git = Get-Command git.exe -ErrorAction SilentlyContinue
if ($git) {
    $gitRoot = Split-Path (Split-Path $git.Source -Parent) -Parent
    $candidates += Join-Path $gitRoot 'bin\bash.exe'
}
foreach ($key in @('HKCU:\SOFTWARE\GitForWindows', 'HKLM:\SOFTWARE\GitForWindows')) {
    $installPath = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallPath
    if ($installPath) {
        $candidates += Join-Path $installPath 'bin\bash.exe'
    }
}
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if ($root) {
        $candidates += Join-Path $root 'Git\bin\bash.exe'
        $candidates += Join-Path $root 'Programs\Git\bin\bash.exe'
    }
}
$bash = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $bash) {
    throw 'Git for Windows was not found. Install https://git-scm.com/download/win (including Git Bash), then reopen PowerShell.'
}

$installerArguments = @($Action)
if ($All) { $installerArguments += '--all' }
elseif ($Claude) { $installerArguments += '--claude' }
elseif ($Codex) { $installerArguments += '--codex' }
else { $installerArguments += '--xedoc' }
if ($Source) {
    if (Test-Path -LiteralPath $Source -PathType Container) {
        $Source = (Resolve-Path -LiteralPath $Source).Path.Replace('\', '/')
    }
    $installerArguments += @('--source', $Source)
}
if ($WithContextRuntime) { $installerArguments += '--with-context-runtime' }

$temporaryScript = $null
$previousHome = $env:HOME
try {
    if (-not $env:HOME) { $env:HOME = $env:USERPROFILE }
    $script = $null
    if ($PSScriptRoot) {
        $localScript = Join-Path $PSScriptRoot 'install.sh'
        if (Test-Path -LiteralPath $localScript -PathType Leaf) { $script = $localScript }
    }
    if (-not $script) {
        $temporaryScript = [IO.Path]::GetTempFileName()
        $script = $temporaryScript
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/apohl79/e47-agent-lab/main/install.sh' -OutFile $script
    }
    & $bash --login $script.Replace('\', '/') @installerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "E47 installation failed (exit code $LASTEXITCODE)."
    }
}
finally {
    $env:HOME = $previousHome
    if ($temporaryScript) { Remove-Item -LiteralPath $temporaryScript -Force }
}
