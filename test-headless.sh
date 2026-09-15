#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the lama-cleanup authors
#
# Headless end-to-end test: build an image inside GIMP, make a selection, call
# the plug-in, let it talk to lama-cleaner, then verify the mask, the pasted-back
# pixels and the cleanup behaviour.
#
#   ./test-headless.sh
#   LAMA_URL=http://127.0.0.1:8080 ./test-headless.sh
#
# The test uses its own GIMP user directory (GIMP3_DIRECTORY) so your real GIMP
# profile is never touched.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${LAMA_URL:-http://127.0.0.1:8080}"
REPORT="$HERE/e2e-report.txt"
LOG="$HERE/e2e.log"
GIMP_APP="${GIMP_APP:-org.gimp.GIMP}"

# Flatpak needs a writable XDG_RUNTIME_DIR, otherwise it fails with
# "Failed to allocate instance identifier".
RT="$HERE/.rt"
mkdir -p "$RT"
chmod 700 "$RT"
export XDG_RUNTIME_DIR="$RT"

# Isolated GIMP user directory + the plug-in (GIMP 3 wants it in a subdirectory).
GIMPUSER="$HERE/.gimp-test"
mkdir -p "$GIMPUSER/plug-ins/lama-cleanup"
install -m 0755 "$HERE/lama-cleanup.py" "$GIMPUSER/plug-ins/lama-cleanup/lama-cleanup.py"

# Keep the plug-in's temp files inside the workspace so the generated mask can
# be inspected after the run.
mkdir -p "$HERE/.tmp"
rm -rf "${HERE:?}"/.tmp/lama-cleanup-*
rm -f "$REPORT"

echo "== running the end-to-end test with gimp-console (user dir: $GIMPUSER) =="
set +e
timeout "${TEST_TIMEOUT:-300}" flatpak run \
    --env=GIMP3_DIRECTORY="$GIMPUSER" \
    --env=LAMA_URL="$URL" \
    --env=E2E_REPORT="$REPORT" \
    --env=TMPDIR="$HERE/.tmp" \
    --command=gimp-console "$GIMP_APP" -i \
    --batch-interpreter=python-fu-eval \
    -b "exec(open('$HERE/tests/gimp_e2e.py').read())" \
    --quit >"$LOG" 2>&1
STATUS=$?
set -e
echo "gimp-console exit code: $STATUS (full output in $LOG)"

echo
echo "== report =="
if [[ -f "$REPORT" ]]; then
    cat "$REPORT"
else
    echo "no report was produced: $REPORT"
    echo "-- suspicious lines from the gimp-console output --"
    grep -nE 'lama|python|Error|Traceback|invalid' "$LOG" | tail -40 || tail -40 "$LOG"
    exit 1
fi
