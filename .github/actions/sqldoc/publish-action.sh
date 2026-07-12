#!/usr/bin/env bash
# Mirror this composite action to the standalone htamber1/sqldoc-action repo so
# consumers can reference it as `uses: htamber1/sqldoc-action@v1`.
#
# A GitHub Action referenced as `owner/name@ref` must live at the ROOT of its
# own repository (action.yml at the top level). This script copies the manifest
# there and tags it.
#
# Prereqs: gh CLI authenticated; the target repo may or may not exist yet.
set -euo pipefail

TARGET_REPO="${1:-htamber1/sqldoc-action}"
TAG="${2:-v1}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR="$(mktemp -d)"

echo "Publishing $SRC_DIR -> $TARGET_REPO@$TAG"

# Create the repo if it doesn't exist yet.
if ! gh repo view "$TARGET_REPO" >/dev/null 2>&1; then
  gh repo create "$TARGET_REPO" --public \
    --description "GitHub Action for sqldoc — document, PII-scan, and health-check your database in CI."
fi

git clone "https://github.com/${TARGET_REPO}.git" "$WORKDIR"
cp "$SRC_DIR/action.yml" "$WORKDIR/action.yml"
cp "$SRC_DIR/README.md" "$WORKDIR/README.md"

cd "$WORKDIR"
git add action.yml README.md
if git diff --cached --quiet; then
  echo "No changes to publish."
else
  git commit -m "Update sqldoc action manifest"
  git push origin HEAD
fi

# Move the major-version tag to the latest commit (standard action convention).
git tag -fa "$TAG" -m "sqldoc action $TAG"
git push -f origin "$TAG"

echo "Done. Reference it as: uses: ${TARGET_REPO}@${TAG}"
rm -rf "$WORKDIR"
