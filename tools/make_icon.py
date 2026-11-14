"""Build Reo's icon files from tools/reo_icon_source.png:
reo/ui/assets/reo.png, reo/ui/assets/reo.svg (PNG in an SVG wrapper), reo/ui/assets/reo.ico (window + Reo.exe icon)."""
import base64
import io
from pathlib import Path

from PIL import Image

root = Path(__file__).resolve().parent.parent
src = Image.open(root / "tools" / "reo_icon_source.png").convert("RGBA")
assets = root / "reo" / "ui" / "assets"
big = src.resize((512, 512), Image.LANCZOS)
big.save(assets / "reo.png", optimize=True)
buf = io.BytesIO(); big.save(buf, "PNG", optimize=True)
b64 = base64.b64encode(buf.getvalue()).decode()
(assets / "reo.svg").write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 512 512">'
    f'<image width="512" height="512" href="data:image/png;base64,{b64}"/></svg>', encoding="utf-8")
import sys
sys.path.insert(0, str(root))
from PIL import ImageDraw
from reo.orb_art import gold_gradient
n = 512
disc = gold_gradient(n, n)
m = Image.new("L", (n, n), 0); ImageDraw.Draw(m).ellipse([0, 0, n - 1, n - 1], fill=255)
disc.putalpha(m)
k = int(n * .86); disc.alpha_composite(src.resize((k, k), Image.LANCZOS), ((n - k) // 2, (n - k) // 2))
disc.save(assets / "reo.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icons written")
