# nanocode installer for Windows PowerShell. Everything lives in %USERPROFILE%\.nanocode.
#
#   irm https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master/install.ps1 | iex
#   .\install.ps1 -Uninstall          remove nanocode, keep config, memory and skills
#   .\install.ps1 -Uninstall -Purge   remove everything
#   .\install.ps1 -NoRea              skip REA (optional reverse-engineering tools, needs Node.js 22+)

param([switch]$Uninstall, [switch]$Purge, [switch]$NoRea)

$ErrorActionPreference = "Stop"
$RepoRaw = "https://raw.githubusercontent.com/car-mrazomor/nanocode-agent/master"
$NanoHome = if ($env:NANOCODE_HOME) { $env:NANOCODE_HOME } else { Join-Path $env:USERPROFILE ".nanocode" }
$App = Join-Path $NanoHome "app"
$Venv = Join-Path $NanoHome "venv"
$Bin = Join-Path $NanoHome "bin"
$Tools = Join-Path $NanoHome "tools"
$PyDir = Join-Path $NanoHome "python"
$NodeDir = Join-Path $NanoHome "node"

function Step($msg) { Write-Host "* $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "! $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "x $msg" -ForegroundColor Red; exit 1 }

function Set-UserPath([bool]$Add) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $current) { $current = "" }
    $parts = @($current.Split(";") | Where-Object { $_ -and ($_.TrimEnd("\") -ne $Bin.TrimEnd("\")) })
    if ($Add) { $parts = @($Bin) + $parts }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

if ($Uninstall) {
    foreach ($dir in @($App, $Venv, $Bin, $Tools, $PyDir, $NodeDir)) {
        if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    }
    Set-UserPath $false
    if ($Purge) {
        if (Test-Path $NanoHome) { Remove-Item -Recurse -Force $NanoHome }
        Step "nanocode removed completely"
    } else {
        Step "nanocode removed (config, memory and skills kept in $NanoHome; -Purge deletes them)"
    }
    exit 0
}

Write-Host "nanocode installer (Windows -> $NanoHome)`n"

# Installs a standalone Python into .nanocode\python with uv, without admin rights. Returns its path.
function Install-ManagedPython {
    $uv = Join-Path $Tools "uv.exe"
    $env:UV_PYTHON_INSTALL_DIR = $PyDir
    $env:UV_CACHE_DIR = Join-Path $Tools "cache"
    if (Test-Path $uv) {
        $found = & $uv python find 3.12 2>$null
        if ($LASTEXITCODE -eq 0 -and $found -and $found.StartsWith($PyDir)) { return $found }
    }
    Step "Installing Python 3.12 into $PyDir (via uv, no admin rights needed)"
    if (-not (Test-Path $uv)) {
        New-Item -ItemType Directory -Force -Path $Tools | Out-Null
        $env:UV_UNMANAGED_INSTALL = $Tools
        & ([scriptblock]::Create((Invoke-RestMethod "https://astral.sh/uv/install.ps1"))) *> $null
        Remove-Item Env:UV_UNMANAGED_INSTALL -ErrorAction SilentlyContinue
        if (-not (Test-Path $uv)) { Fail "couldn't download uv. Install Python yourself: winget install Python.Python.3.12" }
    }
    & $uv python install 3.12 --no-bin *> $null
    if ($LASTEXITCODE -ne 0) { Fail "couldn't download Python. Install it yourself: winget install Python.Python.3.12" }
    $found = & $uv python find 3.12
    Remove-Item -Recurse -Force $env:UV_CACHE_DIR -ErrorAction SilentlyContinue
    return $found
}

$Python = $null
$Candidates = if ($env:NANOCODE_MANAGED_PYTHON) { @() } else { @(@("py", "-3"), @("python"), @("python3")) }
foreach ($candidate in $Candidates) {
    $exe = $candidate[0]
    $pre = @($candidate | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $ok = & $exe @pre -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
        if ($ok -eq "True") { $Python = @{ Exe = $exe; Pre = $pre }; break }
    } catch { }
}
if (-not $Python) { $Python = @{ Exe = (Install-ManagedPython); Pre = @() } }
Step "Python: $($Python.Exe)"

New-Item -ItemType Directory -Force -Path $App, $Bin | Out-Null
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { "" }
if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "nanocode.py"))) {
    Copy-Item (Join-Path $ScriptDir "nanocode.py") (Join-Path $App "nanocode.py") -Force
    Copy-Item (Join-Path $ScriptDir "install.ps1") (Join-Path $App "install.ps1") -Force
    Step "Copied nanocode from $ScriptDir"
} else {
    Invoke-WebRequest -UseBasicParsing "$RepoRaw/nanocode.py" -OutFile (Join-Path $App "nanocode.py")
    Invoke-WebRequest -UseBasicParsing "$RepoRaw/install.ps1" -OutFile (Join-Path $App "install.ps1")
    Step "Downloaded nanocode"
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
if ((Test-Path $Venv) -and -not (Test-Path $VenvPython)) { Remove-Item -Recurse -Force $Venv }
if (-not (Test-Path $Venv)) {
    & $Python.Exe @($Python.Pre) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "couldn't create a Python venv in $Venv" }
}

$env:PIP_USER = "0"
$env:PIP_CACHE_DIR = Join-Path $NanoHome ".pip-cache"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
Remove-Item Env:PIP_TARGET, Env:PIP_PREFIX -ErrorAction SilentlyContinue

Step "Installing dependencies (httpx, rich, prompt_toolkit)"
& $VenvPython -m pip install --quiet --upgrade pip 2>$null | Out-Null
& $VenvPython -m pip install --quiet --upgrade "httpx>=0.27" "rich>=13" "prompt_toolkit>=3.0.40"
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (see the error above)" }
Step "Installing claude-agent-sdk (for the Claude subscription provider)"
& $VenvPython -m pip install --quiet --upgrade claude-agent-sdk 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "claude-agent-sdk failed to install; OpenRouter and Ollama still work" }

$Launcher = Join-Path $Bin "nanocode.cmd"
Set-Content -Path $Launcher -Encoding ASCII -Value "@echo off`r`nrem nanocode launcher (made by the nanocode installer)`r`n`"$VenvPython`" `"$(Join-Path $App 'nanocode.py')`" %*"
Remove-Item -Recurse -Force $env:PIP_CACHE_DIR -ErrorAction SilentlyContinue
Set-UserPath $true
if (-not ($env:Path.Split(";") -contains $Bin)) { $env:Path = "$Bin;$env:Path" }
Step "Command: nanocode ($Launcher)"

New-Item -ItemType Directory -Force -Path (Join-Path $NanoHome "skills") | Out-Null
$Memory = Join-Path $NanoHome "NANOCODE.md"
if (-not (Test-Path $Memory)) {
    $Name = ""
    if (-not [Console]::IsInputRedirected) {
        $Name = Read-Host "Your name, so nanocode can greet you (Enter to skip)"
    }
    $NameLine = if ($Name) { "- Name: $Name" } else { "<!-- - Name: your name -->" }
    $Text = "# Memory`n`nWhat nanocode should always know about you, in every project. Edit freely.`n" +
            "Project notes go in <project>\.nanocode\memory.md (type '# note' in nanocode).`n`n" +
            "## About me`n$NameLine`n`n## Preferences`n"
    [IO.File]::WriteAllText($Memory, $Text)
    Step "Created memory ($Memory)"
}

# REA (https://github.com/morluto/rea): optional reverse-engineering tools, always the latest version.
# Any problem here only skips REA; nanocode itself is already installed.
function Test-Node($exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try { return ([int](& $exe -p "process.versions.node.split('.')[0]") -ge 22) } catch { return $false }
}

# Node.js for REA without admin rights: the official LTS zip from nodejs.org, checksum-verified, into .nanocode\node.
function Install-PrivateNode {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
    Step "Installing Node.js for REA into $NodeDir (official LTS, no admin rights needed)"
    $index = Invoke-RestMethod "https://nodejs.org/dist/index.json"
    $release = $index | Where-Object { $_.lts -and ([int]($_.version.TrimStart("v").Split(".")[0]) -ge 22) } | Select-Object -First 1
    if (-not $release) { throw "no Node.js LTS release found" }
    $version = $release.version
    $name = "node-$version-win-$arch"
    $download = Join-Path $NanoHome ".node-download"
    Remove-Item -Recurse -Force $download -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $download | Out-Null
    $zip = Join-Path $download "$name.zip"
    Invoke-WebRequest -UseBasicParsing "https://nodejs.org/dist/$version/$name.zip" -OutFile $zip
    $sums = (Invoke-WebRequest -UseBasicParsing "https://nodejs.org/dist/$version/SHASUMS256.txt").Content
    $expected = ($sums -split "`n" | Where-Object { $_ -match "  $([regex]::Escape("$name.zip"))$" } | Select-Object -First 1).Split(" ")[0]
    if ((Get-FileHash -Algorithm SHA256 $zip).Hash -ne $expected.ToUpper()) { throw "the Node.js download didn't verify" }
    Expand-Archive -Path $zip -DestinationPath $download -Force
    Remove-Item -Recurse -Force $NodeDir -ErrorAction SilentlyContinue
    Move-Item (Join-Path $download $name) $NodeDir
    Remove-Item -Recurse -Force $download -ErrorAction SilentlyContinue
    Step "Node.js $version ready (used only by REA)"
}

function Install-Rea {
    $config = Join-Path $NanoHome "config.json"
    if ($NoRea -or $env:NANOCODE_NO_REA -or ((Test-Path $config) -and (Select-String -Quiet -Path $config -Pattern '"rea": false'))) { return }
    $nodeExe = $null
    $npmExe = $null
    $systemNode = Get-Command node -ErrorAction SilentlyContinue
    $systemNpm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $env:NANOCODE_PRIVATE_NODE -and $systemNode -and $systemNpm -and (Test-Node $systemNode.Source)) {
        $nodeExe = $systemNode.Source
        $npmExe = $systemNpm.Source
    } else {
        if (-not (Test-Node (Join-Path $NodeDir "node.exe"))) {
            try { Install-PrivateNode } catch { Warn "REA skipped: couldn't install Node.js ($($_.Exception.Message))"; return }
        }
        $nodeExe = Join-Path $NodeDir "node.exe"
        $npmExe = Join-Path $NodeDir "npm.cmd"
    }
    $node = @{ Source = $nodeExe }
    $npm = @{ Source = $npmExe }
    $env:Path = "$(Split-Path $nodeExe);$env:Path"
    $rea = Join-Path $Tools "rea"
    New-Item -ItemType Directory -Force -Path $rea | Out-Null
    Step "Installing REA (reverse-engineering tools, latest; skip with -NoRea)"
    $log = Join-Path $rea "install.log"
    $env:npm_config_cache = Join-Path $rea ".npm-cache"
    & $npm.Source install --prefix $rea --no-audit --no-fund --loglevel=error rea-agents@latest *> $log
    $npmExit = $LASTEXITCODE
    Remove-Item -Recurse -Force $env:npm_config_cache -ErrorAction SilentlyContinue
    Remove-Item Env:npm_config_cache -ErrorAction SilentlyContinue
    if ($npmExit -ne 0) { Warn "REA failed to install (log: $log); nanocode works without it"; return }
    $entry = Join-Path $rea "node_modules\rea-agents\scripts\rea.mjs"
    $version = & $node.Source $entry --version 2>$null | Select-Object -First 1
    if (-not $version) { Warn "REA installed but doesn't start (log: $log); nanocode works without it"; return }
    @{ node = $node.Source; entry = $entry; version = "$version" } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $rea "runtime.json")
    Step "REA $version ready (loaded only when a task needs it)"
}
try { Install-Rea } catch { Warn "REA skipped: $($_.Exception.Message)" }

& $Launcher --version | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "nanocode doesn't start; run $Launcher --version to see why" }

Write-Host "`nnanocode is installed. Open a new terminal, then run it in any project folder: nanocode" -ForegroundColor Green
Write-Host "The first start asks for a provider: Claude subscription, OpenRouter, or Ollama (local)."
Write-Host "Later: nanocode setup | nanocode doctor | nanocode update | nanocode uninstall"
