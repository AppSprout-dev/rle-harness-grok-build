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

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker CLI not found. Install Docker Desktop and retry."
    exit 127
}
docker info *>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker CLI is present but the daemon is not running. Start Docker Desktop."
    exit 1
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
    "run", "--rm",
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

$dockerArgs += @($Image) + $out
& docker @dockerArgs
exit $LASTEXITCODE
