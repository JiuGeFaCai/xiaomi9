#!/usr/bin/env bash
# Prepare a fixed-key cache replacement; actions/cache/save uploads it next.
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${CACHE_KEY:?CACHE_KEY is required}"
: "${CACHE_REF:?CACHE_REF is required}"
: "${CCACHE_DIR:?CCACHE_DIR is required}"
: "${CCACHE_MAXSIZE:?CCACHE_MAXSIZE is required}"

if [ ! -d "$CCACHE_DIR" ]; then
  echo "::warning::本地 ccache 目录不存在，保留远端缓存"
  exit 1
fi

# Recalculate usage and enforce the limit before removing any remote cache.
ccache --cleanup

# Finish pagination before deleting anything. The API's key filter is a prefix:
# oneplus_13 must not delete oneplus_13_b, nor a fallback from another branch.
cache_entries=$(gh api --method GET "repos/$GITHUB_REPOSITORY/actions/caches" \
  -f "key=$CACHE_KEY" -f "ref=$CACHE_REF" -F per_page=100 --paginate)
cache_ids=$(printf '%s\n' "$cache_entries" | jq -r \
  --arg key "$CACHE_KEY" --arg ref "$CACHE_REF" \
  '.actions_caches[] | select(.key == $key and .ref == $ref) | .id')

if [ -z "$cache_ids" ]; then
  echo "当前分支没有旧缓存，准备首次保存: $CACHE_KEY ($CACHE_REF)"
  exit 0
fi

while IFS= read -r cache_id; do
  echo "删除旧缓存 $cache_id: $CACHE_KEY ($CACHE_REF)"
  gh api --method DELETE "repos/$GITHUB_REPOSITORY/actions/caches/$cache_id"
done <<< "$cache_ids"

echo "旧缓存已删除，准备保存更新后的 ccache"
