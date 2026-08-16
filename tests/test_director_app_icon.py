import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "phone_site" / "icons"

BACKGROUND = (28, 29, 31)
WHITE = (255, 255, 255)
RED = (239, 68, 68)


def test_director_app_icons_use_the_requested_three_by_three_mark():
    for size in (180, 192, 512):
        image = Image.open(ICON_DIR / f"aios-icon-{size}.png").convert("RGB")
        assert image.size == (size, size)
        assert image.getpixel((0, 0)) == BACKGROUND

        scale = size / 512
        colors = []
        for index in range(9):
            row, column = divmod(index, 3)
            x = round((96 + column * 116 + 44) * scale)
            y = round((96 + row * 116 + 44) * scale)
            colors.append(image.getpixel((x, y)))

        assert colors == [WHITE, WHITE, WHITE, WHITE, RED, WHITE, RED, WHITE, WHITE]


def test_manifest_and_apple_touch_icon_use_the_generated_assets():
    manifest = json.loads((ROOT / "phone_site" / "manifest.webmanifest").read_text(encoding="utf-8"))
    sources = {icon["src"] for icon in manifest["icons"]}

    assert "/icons/aios-icon-192.png" in sources
    assert "/icons/aios-icon-512.png" in sources
    assert (ICON_DIR / "aios-icon-180.png").is_file()
    assert (ICON_DIR / "aios-icon.ico").is_file()
