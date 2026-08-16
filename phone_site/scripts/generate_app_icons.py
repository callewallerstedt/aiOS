"""Generate the aiOS Director app icons from the canonical 3x3 mark."""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "icons"

BACKGROUND = "#1c1d1f"
WHITE = "#ffffff"
RED = "#ef4444"

# Coordinates are defined on the 512 px master. The mark stays inside the
# maskable-icon safe zone while remaining readable at notification sizes.
MASTER_SIZE = 512
CELL_SIZE = 88
CELL_GAP = 28
CELL_RADIUS = 10
GRID_ORIGIN = (MASTER_SIZE - (CELL_SIZE * 3 + CELL_GAP * 2)) // 2
RED_CELLS = {4, 6}  # center and bottom-left


def render_icon(size: int) -> Image.Image:
    supersample = 4
    canvas_size = size * supersample
    scale = canvas_size / MASTER_SIZE
    image = Image.new("RGB", (canvas_size, canvas_size), BACKGROUND)
    draw = ImageDraw.Draw(image)

    for index in range(9):
        row, column = divmod(index, 3)
        x = (GRID_ORIGIN + column * (CELL_SIZE + CELL_GAP)) * scale
        y = (GRID_ORIGIN + row * (CELL_SIZE + CELL_GAP)) * scale
        side = CELL_SIZE * scale
        draw.rounded_rectangle(
            (round(x), round(y), round(x + side), round(y + side)),
            radius=round(CELL_RADIUS * scale),
            fill=RED if index in RED_CELLS else WHITE,
        )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in (180, 192, 512):
        render_icon(size).save(ICON_DIR / f"aios-icon-{size}.png", optimize=True)

    render_icon(256).save(
        ICON_DIR / "aios-icon.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
