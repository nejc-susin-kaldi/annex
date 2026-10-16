#!/usr/bin/env bash
# The runtime helpers (annex_runtime.py / .js) have one source, runtime/. Templates,
# examples and test fixtures carry copies, because each app folder must build on its own.
# Usage: scripts/sync-runtime.sh          copy runtime/ over every copy
#        scripts/sync-runtime.sh --check  exit 1 if any copy differs (run by test-all.sh)
source "$(dirname "$0")/lib.sh"
check=; [ "${1:-}" = --check ] && check=1
stale=0
for name in annex_runtime.py annex_runtime.js; do
  while IFS= read -r copy; do
    if ! cmp -s "$ROOT/runtime/$name" "$copy"; then
      if [ -n "$check" ]; then echo "differs from runtime/$name: ${copy#"$ROOT"/}"; stale=1
      else cp "$ROOT/runtime/$name" "$copy"; echo "updated ${copy#"$ROOT"/}"; fi
    fi
  done < <(find "$ROOT/templates" "$ROOT/examples" "$ROOT/tests" -name "$name" -not -path '*/node_modules/*')
done
[ -z "$check" ] || [ "$stale" -eq 0 ] || { echo "Run scripts/sync-runtime.sh to update them." >&2; exit 1; }
[ -n "$check" ] && echo "runtime helpers: every copy matches runtime/"
exit 0
