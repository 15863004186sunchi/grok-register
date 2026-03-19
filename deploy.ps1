$ErrorActionPreference = "Stop"

param(
  [ValidateSet("deploy","logs","stop","status","help")]
  [string]$Action = "deploy",
  [string]$Service = ""
)

$repoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoDir

function Show-Usage {
  Write-Host "Usage:"
  Write-Host "  .\deploy.ps1 -Action deploy|logs|stop|status [-Service <name>]"
  Write-Host ""
  Write-Host "Actions:"
  Write-Host "  deploy   Build and start services (default)"
  Write-Host "  logs     Follow logs (optional: service name)"
  Write-Host "  stop     Stop and remove services"
  Write-Host "  status   Show service status"
}

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

switch ($Action) {
  "deploy" {
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
  }
  "logs" {
    if ($Service) {
      & $compose logs -f --tail 200 $Service
    } else {
      & $compose logs -f --tail 200
    }
  }
  "stop" {
    & $compose down
  }
  "status" {
    & $compose ps
  }
  "help" {
    Show-Usage
  }
  default {
    Write-Error "Unknown action: $Action"
    Show-Usage
    exit 1
  }
}