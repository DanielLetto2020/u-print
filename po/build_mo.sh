#!/usr/bin/env bash
# Compile every .po next to it into a matching .mo. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
for po in */LC_MESSAGES/photoprint.po; do
    mo="${po%.po}.mo"
    msgfmt -o "$mo" "$po"
    echo "  $mo"
done
