<#
.SYNOPSIS
  Run the Cura API for the mobile app and the pitch: migrate, seed the demo, serve on 0.0.0.0:8000.

.EXAMPLE
  ./scripts/serve-demo.ps1
  ./scripts/serve-demo.ps1 -PublicUrl https://cura-demo.trycloudflare.com
  ./scripts/serve-demo.ps1 -SeedRegistry never      # skip loading data/*.jsonl

  The JWT secret is generated once into var/.secret (gitignored) unless CURA_JWT_SECRET is set.
  Demo phones 0790000001-3 accept the code 246810 (CURA_DEMO_OTP). Never use this in production.
#>
param(
    [string]$PublicUrl = $env:CURA_PUBLIC_BASE_URL,
    [int]$Port = 8000,
    [ValidateSet('auto', 'always', 'never')]
    [string]$SeedRegistry = 'auto'
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$cmd = @('run', 'cura', 'serve-demo', '--host', '0.0.0.0', '--port', "$Port", '--seed-registry', $SeedRegistry)
if ($PublicUrl) { $cmd += @('--public-url', $PublicUrl) }
& uv @cmd
exit $LASTEXITCODE
