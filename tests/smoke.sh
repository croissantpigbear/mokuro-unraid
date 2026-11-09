#!/usr/bin/env bash
# Runs inside the built image (CI). Processes one generated page on the CPU and
# checks a .mokuro file comes out, so a broken dependency can't be published.
set -euo pipefail

work=$(mktemp -d)
mkdir -p "$work/vol1"

python - "$work/vol1/001.png" <<'EOF'
import sys
from PIL import Image, ImageDraw
im = Image.new("RGB", (800, 1200), "white")
d = ImageDraw.Draw(im)
d.ellipse([250, 150, 550, 650], outline="black", width=6)   # an empty speech bubble
for x in (330, 400, 470):                                  # a few vertical "text" strokes
    d.rectangle([x, 260, x + 18, 540], fill="black")
im.save(sys.argv[1])
EOF

mokuro --disable_confirmation=True --force_cpu=True --legacy_html=False "$work/vol1"

if [ -s "$work/vol1.mokuro" ]; then
  echo "SMOKE TEST: $(wc -c < "$work/vol1.mokuro") byte .mokuro file written"
else
  echo "SMOKE TEST FAILED: no .mokuro file was produced" >&2
  exit 1
fi

# Second run with HTML on: what the web UI does to add HTML to an already-processed volume
mokuro --disable_confirmation=True --force_cpu=True --legacy_html=True "$work/vol1"

if [ -s "$work/vol1.html" ] && grep -q "vol1/001.png" "$work/vol1.html"; then
  echo "SMOKE TEST: $(wc -c < "$work/vol1.html") byte .html file written"
else
  echo "SMOKE TEST FAILED: no usable .html file was produced" >&2
  exit 1
fi

echo "SMOKE TEST PASSED"
