#!/usr/bin/env bash
# Install the repo's git hooks. Run once after cloning:  ./scripts/install-hooks.sh
#
# Git hooks are not themselves version controlled, so the real script lives in
# scripts/ and this symlinks it into .git/hooks.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

mkdir -p .git/hooks
ln -sf ../../scripts/pre-commit .git/hooks/pre-commit
chmod +x scripts/pre-commit

echo "✓ pre-commit hook installed"

if python3 -c "import detect_secrets" 2>/dev/null; then
  echo "✓ detect-secrets present ($(python3 -m detect_secrets --version))"
else
  echo "! detect-secrets is NOT installed - the hook will warn instead of scanning."
  echo "  Install it with:  python3 -m pip install --user detect-secrets"
fi

[ -f .secrets.baseline ] && echo "✓ .secrets.baseline found" \
  || echo "! no .secrets.baseline - create one with: python3 -m detect_secrets scan --all-files > .secrets.baseline"
