#!/bin/zsh
# Tag a release; .github/workflows/release.yml then builds, checks and publishes it.
#
#   scripts/release.sh --dry-run 0.2.0   # run every check and a local build, tag nothing
#   scripts/release.sh 0.2.0             # same, then create and push tag v0.2.0
#
# Before: set the version (`uv version 0.2.0`), rename the CHANGELOG "Unreleased" heading to
# "[0.2.0]", commit, and merge to main. Safe to re-run: an existing tag on HEAD is reused.
set -euo pipefail
cd "${0:A:h}/.."

die() { print -u2 -- "release: $*"; exit 1; }

dry=0
if [[ "${1:-}" == "--dry-run" ]]; then dry=1; shift; fi
[[ $# -eq 1 ]] || die "usage: scripts/release.sh [--dry-run] X.Y.Z"
v="$1"
[[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z (got '$v')"
tag="v$v"

[[ "$(git branch --show-current)" == main ]] || die "not on main"
[[ -z "$(git status --porcelain)" ]] || die "working tree not clean"
git fetch --quiet origin main --tags
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || die "main is not in sync with origin/main"

proj_v="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
[[ "$proj_v" == "$v" ]] || die "pyproject.toml version is $proj_v, not $v (run: uv version $v)"
grep -qF "## [$v]" CHANGELOG.md || die "CHANGELOG.md has no '## [$v]' section"

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  [[ "$(git rev-list -n1 "$tag")" == "$(git rev-parse HEAD)" ]] || die "$tag already exists on another commit"
  print "release: $tag already exists on HEAD; reusing it"
fi

scripts/test.sh

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
uv build --quiet --out-dir "$tmp"
python3 scripts/check_dist.py "$tmp" "$v"

if (( dry )); then
  print "release: dry run ok; would tag $tag at $(git rev-parse --short HEAD) and push it"
  exit 0
fi

git rev-parse -q --verify "refs/tags/$tag" >/dev/null || git tag -a "$tag" -m "inkwell $v"
git push origin "$tag"
print "release: pushed $tag; watch it with: gh run watch \$(gh run list -w release -L1 --json databaseId -q '.[0].databaseId')"
