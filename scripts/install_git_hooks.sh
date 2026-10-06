#!/usr/bin/env bash
# Installs the git hooks that run AgenticBotPlatform's local CI/CD pipeline (pre-push) and the
# public-repo leak guard (pre-commit). .git/hooks isn't version-controlled, so this copies the
# tracked sources in scripts/git-hooks/ into place — re-run any time to pick up hook changes.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$root/.git/hooks"
for hook in pre-commit pre-push; do
  cp "$root/scripts/git-hooks/$hook" "$root/.git/hooks/$hook"
  chmod +x "$root/.git/hooks/$hook"
done

echo "Installed pre-commit hook — every commit now passes the public-repo leak guard."
echo "Installed pre-push hook — every 'git push' now runs the local CI/CD pipeline first."
echo "Run the guard over the whole tree any time with: python scripts/leak_guard.py"
echo "Run the pipeline directly any time with: python scripts/local_pipeline.py"