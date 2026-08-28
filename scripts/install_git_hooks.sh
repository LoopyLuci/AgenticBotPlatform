#!/usr/bin/env bash
# Installs the pre-push hook that runs BotServer's local CI/CD pipeline
# (scripts/local_pipeline.py) before every push. .git/hooks isn't
# version-controlled, so this copies the tracked source in
# scripts/git-hooks/ into place — re-run any time to pick up hook changes.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cp "$root/scripts/git-hooks/pre-push" "$root/.git/hooks/pre-push"
chmod +x "$root/.git/hooks/pre-push"

echo "Installed pre-push hook — every 'git push' now runs the local CI/CD pipeline first."
echo "Run it directly any time with: python scripts/local_pipeline.py"
