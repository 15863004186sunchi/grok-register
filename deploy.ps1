$ErrorActionPreference = "Stop"

$repoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoDir

# Pick compose command
$compose = @()
try {
  docker compose version | Out-Null
  $compose = @("docker", "compose")
} catch {
  if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    $compose = @("docker-compose")
  } else {
    Write-Error "Docker Compose not found. Install Docker Desktop or docker-compose."
    exit 1
  }
}

# Ensure config.json exists
if (!(Test-Path "config.json")) {
  if (Test-Path "config.example.json") {
    Copy-Item "config.example.json" "config.json"
    Write-Warning "Created config.json from config.example.json. Please edit it before running."
  } else {
    Write-Error "Missing config.example.json to create config.json."
    exit 1
  }
}

New-Item -ItemType Directory -Force -Path "sso" | Out-Null
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

& $compose up -d --build
& $compose ps

Write-Host "Deploy complete."