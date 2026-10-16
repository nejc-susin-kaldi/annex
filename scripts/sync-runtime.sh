#!/usr/bin/env bash
# The runtime helpers (annex_runtime.py / .js, annex-errors.js) have one source, runtime/. Templates,
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
# the browser error script (M2.3) goes next to the runtime in every web app folder, and into the static template's public/
copies=()
while IFS= read -r rt; do
  dir=$(dirname "$rt")
  if [ -f "$dir/main.py" ] || [ -f "$dir/server.js" ]; then copies+=("$dir/annex-errors.js"); fi
done < <(find "$ROOT/templates" "$ROOT/examples" "$ROOT/tests" \( -name annex_runtime.py -o -name annex_runtime.js \) -not -path '*/node_modules/*')
copies+=("$ROOT/templates/static/public/annex-errors.js")
for copy in "${copies[@]}"; do
  if ! cmp -s "$ROOT/runtime/annex-errors.js" "$copy"; then
    if [ -n "$check" ]; then echo "differs from runtime/annex-errors.js: ${copy#"$ROOT"/}"; stale=1
    else cp "$ROOT/runtime/annex-errors.js" "$copy"; echo "updated ${copy#"$ROOT"/}"; fi
  fi
done
[ -z "$check" ] || [ "$stale" -eq 0 ] || { echo "Run scripts/sync-runtime.sh to update them." >&2; exit 1; }
[ -n "$check" ] && echo "runtime helpers: every copy matches runtime/"
exit 0
