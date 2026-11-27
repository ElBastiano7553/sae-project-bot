# Lance Claude Code avec le pont Discord (plugin officiel discord@claude-plugins-official).
# Ne stocke ni n'affiche aucun token : le token est configuré une fois avec /discord:configure.
# Le pont ne fonctionne que tant que cette fenêtre reste ouverte.
param(
    [string]$Dossier = (Get-Location).Path
)
$ErrorActionPreference = "Stop"

if (-not (Get-Command bun -ErrorAction SilentlyContinue)) {
    Write-Host "Bun est introuvable. Installez-le : powershell -c `"irm bun.sh/install.ps1 | iex`", puis rouvrez PowerShell." -ForegroundColor Red
    exit 1
}
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "Claude Code (commande 'claude') est introuvable." -ForegroundColor Red
    exit 1
}
$envFile = Join-Path $env:USERPROFILE ".claude\channels\discord\.env"
if (-not (Test-Path $envFile)) {
    Write-Host "Token non configuré : lancez 'claude', puis /plugin install discord@claude-plugins-official et /discord:configure <token>." -ForegroundColor Yellow
    exit 1
}
$access = Join-Path $env:USERPROFILE ".claude\channels\discord\access.json"
if (-not (Test-Path $access) -or -not ((Get-Content $access -Raw) -match '"dmPolicy"\s*:\s*"allowlist"')) {
    Write-Host "Attention : la politique d'accès n'est pas 'allowlist'. Après l'appairage, tapez /discord:access policy allowlist." -ForegroundColor Yellow
}
if (-not (Test-Path $Dossier)) {
    Write-Host "Dossier introuvable : $Dossier" -ForegroundColor Red
    exit 1
}
Set-Location $Dossier
Write-Host "Bun $(bun --version) ; $(claude --version). Pont Discord actif tant que cette fenêtre reste ouverte." -ForegroundColor Green
claude --channels plugin:discord@claude-plugins-official
