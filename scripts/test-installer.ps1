#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('e47 installer ' + [guid]::NewGuid())
$savedEnvironment = @{}
foreach ($name in @('HOME', 'PATH', 'XEDOC_HOME', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'E47_INSTALLER_TEST_LOG', 'E47_MARKETPLACE_SOURCE')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
}

function Assert-Logged([string]$Expected) {
    if (-not (Select-String -LiteralPath $env:E47_INSTALLER_TEST_LOG -SimpleMatch $Expected -Quiet)) {
        throw "Missing host command: $Expected"
    }
}

try {
    $bin = Join-Path $testRoot 'bin'
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    $env:HOME = Join-Path $testRoot 'home'
    $env:XEDOC_HOME = Join-Path $testRoot 'xedoc'
    $env:XDG_DATA_HOME = Join-Path $testRoot 'data'
    $env:XDG_CONFIG_HOME = Join-Path $testRoot 'config'
    $env:XDG_CACHE_HOME = Join-Path $testRoot 'cache'
    $env:E47_INSTALLER_TEST_LOG = (Join-Path $testRoot 'host.log').Replace('\', '/')
    $env:E47_MARKETPLACE_SOURCE = $null
    $env:PATH = "$bin;$env:PATH"
    New-Item -ItemType Directory -Path $env:HOME -Force | Out-Null
    foreach ($hostName in @('xedoc', 'claude', 'codex')) {
        [IO.File]::WriteAllText((Join-Path $bin $hostName), "#!/usr/bin/env bash`nprintf '%s %s\n' '$hostName' `"`$*`" >>`"`$E47_INSTALLER_TEST_LOG`"`n")
    }

    & "$repo/install.ps1" -Source $repo
    Assert-Logged 'xedoc plugin add matrix@e47'
    if (Select-String -LiteralPath $env:E47_INSTALLER_TEST_LOG -Pattern '^(claude|codex) ' -Quiet) {
        throw 'Default installation selected another host.'
    }
    $launcher = Join-Path $env:HOME '.local/bin/inline-discussion.cmd'
    if (-not (Test-Path -LiteralPath $launcher)) { throw 'Missing Windows launcher.' }
    $help = & $env:ComSpec /d /s /c "`"`"$launcher`" --help 2>&1`""
    # The existing tool's usage path exits 1.
    if ($LASTEXITCODE -ne 1 -or "$help" -notmatch 'Usage:') { throw 'Windows launcher failed.' }

    & "$repo/install.ps1" -Claude -Source $repo
    Assert-Logged 'claude plugin install auto-compaction@e47'
    & "$repo/install.ps1" -Codex -Source $repo
    Assert-Logged 'codex plugin add reviewers@e47'
    & "$repo/install.ps1" -All -Source $repo
    & "$repo/install.ps1" -Action uninstall -Source $repo
    if (Test-Path -LiteralPath $launcher) { throw 'Uninstall left the Windows launcher behind.' }

    $failed = $false
    try { & "$repo/install.ps1" -Codex -Claude } catch { $failed = $true }
    if (-not $failed) { throw 'Conflicting host selectors were accepted.' }

    # Match the README's scriptblock invocation without fetching an old
    # install.sh from main while testing an unmerged change.
    function Invoke-WebRequest {
        param([switch]$UseBasicParsing, $Uri, $OutFile)
        Copy-Item "$repo/install.sh" $OutFile
    }
    Set-Content -LiteralPath $env:E47_INSTALLER_TEST_LOG -Value ''
    & ([scriptblock]::Create((Get-Content -Raw "$repo/install.ps1"))) -Codex -Source $repo
    Assert-Logged 'codex plugin add reviewers@e47'

    # Verify native Bash failures are not reported as successful installations.
    [IO.File]::WriteAllText((Join-Path $bin 'xedoc'), "#!/usr/bin/env bash`nexit 23`n")
    $failed = $false
    try { & "$repo/install.ps1" -Source $repo } catch { $failed = $true }
    if (-not $failed) { throw 'Host failure was ignored.' }
    Write-Host 'PowerShell installer tests passed.'
}
finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name])
    }
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}
