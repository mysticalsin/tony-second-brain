#!/usr/bin/env bash
# render_pdf.sh <input.pptx> <output_dir>
# Uses Microsoft PowerPoint via AppleScript to export to PDF.
#
# REQUIRES one-time PowerPoint setup:
#   1. Open Microsoft PowerPoint manually at least once
#   2. Sign in with your Office account, accept EULA, dismiss splash dialogs
#   3. On first run, macOS will prompt for Automation permission for
#      "Terminal → Microsoft PowerPoint". Click Allow.
#
# If you see "AppleEvent timed out", PowerPoint has a dialog waiting for you.
# Open PowerPoint, dismiss the dialog, retry.
set -euo pipefail

PPTX="${1:?usage: render_pdf.sh <pptx> <out_dir>}"
OUT_DIR="${2:?usage: render_pdf.sh <pptx> <out_dir>}"

if [[ ! -d "/Applications/Microsoft PowerPoint.app" ]]; then
  echo "Microsoft PowerPoint not found at /Applications/" >&2
  exit 127
fi

PPTX_ABS="$(cd "$(dirname "$PPTX")" && pwd)/$(basename "$PPTX")"
OUT_DIR_ABS="$(cd "$OUT_DIR" && pwd)"
BASENAME="$(basename "$PPTX" .pptx)"
PDF_ABS="$OUT_DIR_ABS/$BASENAME.pdf"

rm -f "$PDF_ABS"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ASCRIPT="$SCRIPT_DIR/pptx_to_pdf.applescript"

if [[ ! -f "$ASCRIPT" ]]; then
  echo "Missing AppleScript: $ASCRIPT" >&2
  exit 1
fi

if ! /usr/bin/osascript "$ASCRIPT" "$PPTX_ABS" "$PDF_ABS" >/dev/null; then
  cat >&2 <<'NOTE'

PowerPoint AppleScript failed. Most likely causes:
  • PowerPoint has a dialog open (sign-in, splash, "What's New") — open it
    manually, dismiss, retry.
  • macOS hasn't granted Automation permission — System Settings →
    Privacy & Security → Automation → enable PowerPoint for your shell.
  • PowerPoint isn't signed in.

Workaround: open the .pptx in PowerPoint manually and File → Save As → PDF.
The --qa contact sheet step will pick up the PDF when present.
NOTE
  exit 1
fi

if [[ ! -f "$PDF_ABS" ]]; then
  echo "PowerPoint did not produce PDF at $PDF_ABS" >&2
  exit 1
fi
