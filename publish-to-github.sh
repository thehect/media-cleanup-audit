#!/usr/bin/env bash
set -euo pipefail

REPO_NAME="${1:-media-cleanup-audit}"
VISIBILITY="${VISIBILITY:-private}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Missing GitHub CLI: gh"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI is not authenticated."
  echo "Run: gh auth login -h github.com"
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Run this from the media-cleanup-audit git repository."
  exit 1
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "Remote origin already exists:"
  git remote -v
  git push -u origin main
  exit 0
fi

case "$VISIBILITY" in
  public|private|internal) ;;
  *)
    echo "VISIBILITY must be public, private, or internal"
    exit 1
    ;;
esac

gh repo create "$REPO_NAME" "--$VISIBILITY" --source . --remote origin --push
