#!/usr/bin/env bash
# Prepare (or refresh) a Git mirror the bot can build analysis snapshots from.
#
#   ./scripts/prepare-repo.sh <name> [git-url]
#
# A mirror clone has no working tree, so there is nothing for the analysis agent
# to find even if it ignored its sandbox. Running this on the host keeps the
# repository credentials out of the bot container: pair it with
# ANALYSIS_ALLOW_FETCH=false and a `:ro` mount of ./repos.
#
# The matching config.yaml entry is:
#   projects:
#     <name>:
#       sentry_project_slug: <sentry project slug>
#       repo_dir: /workspace/repos/<name>
set -euo pipefail

name=${1:-}
url=${2:-}

if [[ -z "$name" ]]; then
    echo "usage: $0 <name> [git-url]" >&2
    exit 2
fi
if [[ ! "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "error: invalid name: $name" >&2
    exit 2
fi

root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$root/repos" "$root/state/codex" "$root/state/data" "$root/state/snapshots"
target="$root/repos/$name"

if [[ -d "$target" ]]; then
    echo "==> refreshing $target"
    git -C "$target" fetch --prune --tags origin
else
    if [[ -z "$url" ]]; then
        echo "error: $target does not exist yet, so a git-url is required" >&2
        exit 2
    fi
    echo "==> mirroring $url into $target"
    # Full history: `git archive <sha>` must be able to reach any deployed commit.
    git clone --mirror "$url" "$target"
fi

echo "==> HEAD: $(git -C "$target" rev-parse --short HEAD 2>/dev/null || echo 'n/a')"
echo "==> done. config.yaml 의 repo_dir 를 /workspace/repos/$name 로 설정하세요."
