"""Pull Reo's avatar out of the original Reo_UI.html so the app uses it.

    python tools/extract_avatar.py path\to\Reo_UI.html

Writes reo/ui/assets/reo.svg (the image embedded in an SVG wrapper, cropped like object-fit: cover).
"""
import re
import sys
from pathlib import Path

src = Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r'src="(data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+)"', src)
if not m:
    sys.exit("No embedded avatar image found in that file.")
svg = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 512 512">'
       f'<image width="512" height="512" preserveAspectRatio="xMidYMid slice" href="{m.group(1)}"/></svg>')
out = Path(__file__).resolve().parent.parent / "reo" / "ui" / "assets" / "reo.svg"
out.write_text(svg, encoding="utf-8")
print("Wrote", out)
