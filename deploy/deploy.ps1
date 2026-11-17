$ErrorActionPreference = 'Stop'

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvFile = if ($env:AETHERION_ENV_FILE) { $env:AETHERION_ENV_FILE } else { Join-Path $RootDir 'deploy\production.env' }
$ComposeFile = Join-Path $RootDir 'deploy\compose\docker-compose.prod.yml'

function Fail([string]$Message) {
    Write-Error "[FAIL] $Message"
    exit 1
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail 'python is required' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker is required' }
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose v2 is required' }

function New-HexSecret {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return ([System.BitConverter]::ToString($buffer) -replace '-', '').ToLowerInvariant()
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath (Join-Path $RootDir 'deploy\production.env.example') -Destination $EnvFile
    $text = Get-Content -LiteralPath $EnvFile -Raw
    $text = $text.Replace('REPLACE_WITH_INDEPENDENT_64_CHAR_HEX_SECRET_KEY', (New-HexSecret))
    $text = $text.Replace('REPLACE_WITH_INDEPENDENT_64_CHAR_HEX_JWT_SECRET', (New-HexSecret))
    $postgresSecret = New-HexSecret
    $text = $text.Replace('REPLACE_WITH_64_CHAR_HEX_POSTGRES_PASSWORD', $postgresSecret)
    $text = $text.Replace('REPLACE_WITH_SECURE_POSTGRES_PASSWORD', $postgresSecret)
    Set-Content -LiteralPath $EnvFile -Value $text -Encoding UTF8
    Write-Host "[INFO] Created and initialized $EnvFile"
}

$envMap = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $envMap[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
        Set-Item -Path "Env:$($Matches[1])" -Value $envMap[$Matches[1]]
    }
}

foreach ($required in @('SECRET_KEY', 'JWT_SECRET', 'POSTGRES_PASSWORD')) {
    if (-not $envMap.ContainsKey($required) -or [string]::IsNullOrWhiteSpace($envMap[$required]) -or $envMap[$required] -like 'REPLACE_*') {
        Fail "$required is missing or still a placeholder"
    }
}

python (Join-Path $RootDir 'scripts\production_preflight.py') --env-file $EnvFile --config
docker compose --env-file $EnvFile -f $ComposeFile config --quiet
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose configuration is invalid' }
docker compose --env-file $EnvFile -f $ComposeFile up -d --build
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose startup failed' }

$ready = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    docker compose --env-file $EnvFile -f $ComposeFile exec -T postgres pg_isready -U $envMap['POSTGRES_USER'] -d $envMap['POSTGRES_DB'] *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[PASS] PostgreSQL ready after $attempt attempt(s)"
        $ready = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $ready) { Fail 'PostgreSQL did not become ready' }

docker compose --env-file $EnvFile -f $ComposeFile run --rm db-migrate python /app/scripts/db_migrate.py --up
if ($LASTEXITCODE -ne 0) { Fail 'Database migration failed' }
python (Join-Path $RootDir 'scripts\production_preflight.py') --env-file $EnvFile --runtime --base-url http://127.0.0.1:5000
if ($LASTEXITCODE -ne 0) { Fail 'Runtime production preflight failed' }

Write-Host '[SUCCESS] Aetherion AI Platform is 100% Deployed and Production-Ready!'
