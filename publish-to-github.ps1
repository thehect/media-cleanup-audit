param(
  [string]$RepoName = "media-cleanup-audit",
  [ValidateSet("public", "private", "internal")]
  [string]$Visibility = "private"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  throw "Missing GitHub CLI: gh"
}

gh auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "GitHub CLI is not authenticated."
  Write-Host "Run: gh auth login -h github.com"
  exit 1
}

git rev-parse --is-inside-work-tree | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "Run this from the media-cleanup-audit git repository."
}

git remote get-url origin | Out-Null
if ($LASTEXITCODE -eq 0) {
  Write-Host "Remote origin already exists:"
  git remote -v
  git push -u origin main
  exit 0
}

gh repo create $RepoName "--$Visibility" --source . --remote origin --push
