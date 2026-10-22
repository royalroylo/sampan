#!/bin/bash
# Host-side driver: build the image from a `git archive` of the repo (no host directories are mounted), run the unit
# suite on Python 3.11/3.12/3.13, then the live smoke on each. Everything tmux-related happens inside the container.
# Usage: scripts/linux-smoke/run.sh [git-ref]      (default: HEAD)
set -euo pipefail
ref="${1:-HEAD}"
repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
git -C "$repo" archive "$ref" | tar -x -C "$ctx"
docker build -q -t zz-sampan-linux-img -f "$ctx/scripts/linux-smoke/Dockerfile" "$ctx"
for py in 3.11 3.12 3.13; do
    echo "##### unit tests, Python $py"
    docker run --rm --name zz-sampan-linux-unit zz-sampan-linux-img uv run --python "$py" python -m unittest discover -s tests -t .
    echo "##### live smoke, Python $py"
    docker run --rm --name zz-sampan-linux-smoke -e SMOKE_PY="$py" zz-sampan-linux-img bash scripts/linux-smoke/smoke.sh
done
echo "##### installer test (bare Ubuntu: install.sh brings uv, tmux, ps, lsof, git; shellcheck; dash)"
docker build -q -t zz-sampan-linux-install-img -f "$ctx/scripts/linux-smoke/Dockerfile.install" "$ctx"
docker run --rm --name zz-sampan-linux-install zz-sampan-linux-install-img bash scripts/linux-smoke/install-test.sh
