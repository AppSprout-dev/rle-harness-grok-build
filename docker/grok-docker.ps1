# Drop-in `grok` that runs the stock Linux binary in Docker (Windows).
# Prefer grok-docker.cmd as --harness-opt binary= so CreateProcess can exec it.
# Requires Docker Desktop with the daemon running.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$GrokArgs
)

$ErrorActionPreference = "Stop"
$Image = if ($env:GROK_DOCKER_IMAGE) { $env:GROK_DOCKER_IMAGE } else { "rle-grok-build:local" }
$McpUrl = if ($env:MCP_URL) { $env:MCP_URL } else { "http://host.docker.internal:8766/mcp" }

# Windows cmd.exe %* drops quoted/large -p prompts. The harness writes argv
# (after the wrapper path) as UTF-8 JSON and sets RLE_GROK_ARGV_JSON.
if (-not [string]::IsNullOrWhiteSpace($env:RLE_GROK_ARGV_JSON)) {
    if (-not (Test-Path -LiteralPath $env:RLE_GROK_ARGV_JSON)) {
        Write-Error "RLE_GROK_ARGV_JSON is set but file not found: $($env:RLE_GROK_ARGV_JSON)"
        exit 1
    }
    $raw = [System.IO.File]::ReadAllText(
        $env:RLE_GROK_ARGV_JSON,
        [System.Text.UTF8Encoding]::new($false)
    )
    if (-not $raw.Trim().StartsWith("[")) {
        Write-Error "RLE_GROK_ARGV_JSON must be a JSON array of strings"
        exit 1
    }
    $parsed = ConvertFrom-Json -InputObject $raw
    if ($null -eq $parsed) {
        $GrokArgs = [string[]]@()
    }
    else {
        $asArray = @($parsed)
        foreach ($item in $asArray) {
            if ($item -isnot [string]) {
                Write-Error "RLE_GROK_ARGV_JSON must be a JSON array of strings"
                exit 1
            }
        }
        $GrokArgs = [string[]]$asArray
    }
}

$PersistAction = $env:RLE_GROK_PERSIST_ACTION
$PersistContainer = $env:RLE_GROK_PERSIST_CONTAINER
if (-not [string]::IsNullOrWhiteSpace($PersistAction)) {
    if (@("start", "exec", "stop") -notcontains $PersistAction) {
        Write-Error "unknown RLE_GROK_PERSIST_ACTION=$PersistAction (expected start|exec|stop)"
        exit 1
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker CLI not found. Install Docker Desktop and retry."
    exit 127
}
docker info *>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker CLI is present but the daemon is not running. Start Docker Desktop."
    exit 1
}

if ($PersistAction -eq "stop") {
    if ([string]::IsNullOrWhiteSpace($PersistContainer)) {
        Write-Error "RLE_GROK_PERSIST_ACTION=stop requires RLE_GROK_PERSIST_CONTAINER"
        exit 1
    }
    $ErrorActionPreference = "Continue"
    docker stop --time 10 $PersistContainer
    docker rm -f $PersistContainer
    exit 0
}

$cwdHost = $null
$out = New-Object System.Collections.Generic.List[string]
for ($i = 0; $i -lt $GrokArgs.Count; $i++) {
    $a = $GrokArgs[$i]
    if ($a -eq "--cwd" -and ($i + 1) -lt $GrokArgs.Count) {
        $cwdHost = $GrokArgs[$i + 1]
        [void]$out.Add("--cwd")
        [void]$out.Add("/work")
        $i++
        continue
    }
    if ($a.StartsWith("--cwd=")) {
        $cwdHost = $a.Substring(6)
        [void]$out.Add("--cwd=/work")
        continue
    }
    [void]$out.Add($a)
}

function Test-HostileTempBindSource {
    param([string]$Candidate)
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return $false
    }
    $normalized = $Candidate.Replace('/', '\').ToLowerInvariant()
    if ($normalized -match '\\appdata\\local\\temp(\\|$)') {
        return $true
    }
    # Harness prefixes: rle-grok-home-* (GROK_HOME) and rle-grok-* (--cwd).
    if ($normalized -match '\\temp\\rle-grok') {
        return $true
    }
    foreach ($root in @($env:TEMP, $env:TMP)) {
        if ([string]::IsNullOrWhiteSpace($root)) {
            continue
        }
        $rootNorm = $root.Replace('/', '\').TrimEnd('\').ToLowerInvariant()
        if ($normalized -eq $rootNorm -or $normalized.StartsWith($rootNorm + '\')) {
            return $true
        }
    }
    return $false
}

function Write-SkipTempMount {
    param([string]$Role, [string]$MountPath)
    Write-Warning ("skipping Docker bind-mount of hostile temp ${Role} '${MountPath}'. " +
        "Docker Desktop on Windows returns exit 125 Access is denied for %TEMP% / AppData\Local\Temp mounts. " +
        "Relying on MCP_URL env and an empty container GROK_HOME (entrypoint writes config). " +
        "Session resume across --rm ticks needs a non-Temp volume (set GROK_DOCKER_HOME_VOLUME).")
}

$dockerArgs = @(
    "--add-host=host.docker.internal:host-gateway",
    "-e", "MCP_URL=$McpUrl",
    "-e", "GROK_HOME=/home/grok/.grok"
)
if ($env:XAI_API_KEY) {
    $dockerArgs += @("-e", "XAI_API_KEY=$($env:XAI_API_KEY)")
}
if ($env:GROK_AUTH_JSON -and (Test-Path -LiteralPath $env:GROK_AUTH_JSON) -and ((Get-Item $env:GROK_AUTH_JSON).Length -gt 0)) {
    $dockerArgs += @("-v", "$($env:GROK_AUTH_JSON):/auth/auth.json:ro")
}

# Named volume persists /home/grok/.grok across --rm ticks without %TEMP%.
if ($env:GROK_DOCKER_HOME_VOLUME) {
    $dockerArgs += @("-v", "$($env:GROK_DOCKER_HOME_VOLUME):/home/grok/.grok")
}
elseif ($env:GROK_HOME -and (Test-Path -LiteralPath $env:GROK_HOME -PathType Container)) {
    $resolvedHome = (Resolve-Path -LiteralPath $env:GROK_HOME).Path
    $hostDefault = Join-Path $HOME ".grok"
    $resolvedDefault = $null
    if (Test-Path -LiteralPath $hostDefault) {
        $resolvedDefault = (Resolve-Path -LiteralPath $hostDefault).Path
    }
    if ($resolvedDefault -and ($resolvedHome -eq $resolvedDefault)) {
        Write-Warning "refusing to mount host ~/.grok (plugin zoo). Using empty container home."
    }
    elseif (Test-HostileTempBindSource $resolvedHome) {
        Write-SkipTempMount -Role "GROK_HOME" -MountPath $resolvedHome
    }
    else {
        $dockerArgs += @("-v", "${resolvedHome}:/home/grok/.grok")
    }
}

if ($cwdHost -and (Test-Path -LiteralPath $cwdHost -PathType Container)) {
    $resolvedCwd = (Resolve-Path -LiteralPath $cwdHost).Path
    if (Test-HostileTempBindSource $resolvedCwd) {
        Write-SkipTempMount -Role "--cwd" -MountPath $resolvedCwd
    }
    else {
        $dockerArgs += @("-v", "${resolvedCwd}:/work")
    }
}

function Write-GrokDockerTrace {
    param([string[]]$Args)
    if (-not $env:RLE_GROK_DOCKER_TRACE) {
        return
    }
    $redacted = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $Args.Count; $i++) {
        $a = $Args[$i]
        if ($i -gt 0 -and $Args[$i - 1] -eq "-p") {
            [void]$redacted.Add("<prompt $($a.Length) chars>")
        }
        elseif ($a -like "XAI_API_KEY=*") {
            [void]$redacted.Add("XAI_API_KEY=<redacted>")
        }
        elseif ($a -like "GROK_AGENT_SECRET=*") {
            [void]$redacted.Add("GROK_AGENT_SECRET=<redacted>")
        }
        else {
            [void]$redacted.Add($a)
        }
    }
    $payload = [string]::Join("`n", $redacted.ToArray())
    $traceDest = $env:RLE_GROK_DOCKER_TRACE
    if ($traceDest -eq "1" -or $traceDest -eq "true") {
        Write-Warning $payload
    }
    else {
        [System.IO.File]::WriteAllText(
            $traceDest,
            $payload + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
    }
}

# INVOKE: PowerShell splat only. Do NOT use Start-Process -ArgumentList.
# Start-Process re-joins the array into one Windows command line; the CRT
# re-splits it. Live prove: Unicode em dash in "RLE turn — tick 0..." became
# a new argument (`error: unexpected argument '—' found`).
if ($PersistAction -eq "exec") {
    if ([string]::IsNullOrWhiteSpace($PersistContainer)) {
        Write-Error "RLE_GROK_PERSIST_ACTION=exec requires RLE_GROK_PERSIST_CONTAINER"
        exit 1
    }
    $execArgs = @(
        "exec",
        "-e", "MCP_URL=$McpUrl",
        "-e", "GROK_HOME=/home/grok/.grok"
    )
    if ($env:XAI_API_KEY) {
        $execArgs += @("-e", "XAI_API_KEY=$($env:XAI_API_KEY)")
    }
    $execArgs += @($PersistContainer, "/entrypoint.sh") + @($out)
    Write-GrokDockerTrace -Args $execArgs
    & docker @execArgs
    exit $LASTEXITCODE
}

if ($PersistAction -eq "start") {
    if ([string]::IsNullOrWhiteSpace($PersistContainer)) {
        Write-Error "RLE_GROK_PERSIST_ACTION=start requires RLE_GROK_PERSIST_CONTAINER"
        exit 1
    }
    $ErrorActionPreference = "Continue"
    docker rm -f $PersistContainer *>$null
    $ErrorActionPreference = "Stop"
    $imageCmd = @("persist")
    if (-not [string]::IsNullOrWhiteSpace($env:RLE_GROK_ACP_PUBLISH)) {
        $dockerArgs += @("-p", $env:RLE_GROK_ACP_PUBLISH)
        if ($env:GROK_AGENT_SECRET) {
            $dockerArgs += @("-e", "GROK_AGENT_SECRET=$($env:GROK_AGENT_SECRET)")
        }
        # --cwd was rewritten for the /work bind-mount; do not forward it
        # (or other grok -p flags) into acp-serve → grok agent (1.0.13 exit 2).
        $agentFlags = New-Object System.Collections.Generic.List[string]
        $skipNext = $false
        $pOnlySwitches = [System.Collections.Generic.HashSet[string]]::new(
            [string[]]@(
                "--no-subagents", "--no-plan", "--yolo", "-p", "--single",
                "--include-partial-messages", "--fork-session", "--continue",
                "-c", "--no-memory", "--disable-web-search", "persist", "acp-serve"
            )
        )
        $pOnlyValues = [System.Collections.Generic.HashSet[string]]::new(
            [string[]]@(
                "--cwd", "--max-turns", "--disallowed-tools", "--output-format",
                "--resume", "-r", "--reasoning-effort", "--effort",
                "--session-id", "-s", "--prompt-json", "--prompt-file",
                "--permission-mode", "--tools"
            )
        )
        foreach ($a in $out) {
            if ($skipNext) {
                $skipNext = $false
                continue
            }
            if ($pOnlySwitches.Contains($a)) {
                continue
            }
            $eq = $a.IndexOf("=")
            $name = if ($eq -ge 0) { $a.Substring(0, $eq) } else { $a }
            if ($pOnlyValues.Contains($name)) {
                if ($eq -lt 0) {
                    $skipNext = $true
                }
                continue
            }
            [void]$agentFlags.Add($a)
        }
        $imageCmd = @("acp-serve") + @($agentFlags)
    }
    $startArgs = @("run", "-d", "--name", $PersistContainer) + $dockerArgs + @($Image) + $imageCmd
    Write-GrokDockerTrace -Args $startArgs
    & docker @startArgs
    exit $LASTEXITCODE
}

$runArgs = @("run", "--rm") + $dockerArgs + @($Image) + @($out)
Write-GrokDockerTrace -Args $runArgs
& docker @runArgs
exit $LASTEXITCODE
