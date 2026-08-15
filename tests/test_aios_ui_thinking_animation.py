import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BEAUTIFUL_CSS = ROOT / "aios_ui" / "web" / "css" / "code-beautiful.css"
BASE_CSS = ROOT / "aios_ui" / "web" / "css" / "code.css"


def _rule(css: str, selector: str) -> str:
    """Return the body of the first top-level block matching ``selector``."""
    at = css.index(selector)
    open_at = css.index("{", at)
    depth = 1
    i = open_at + 1
    while depth:
        ch = css[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return css[open_at + 1 : i - 1]


def test_beautiful_thinking_shimmer_is_superimposed_on_text():
    """The aiOS (beautiful-ui) sheet overrides the base loader, so it must
    carry the full shimmer recipe. The regression was that it animated a
    background gradient but never clipped it to the text, so the opaque
    'Thinking' label painted over it and the animation was invisible."""
    css = BEAUTIFUL_CSS.read_text(encoding="utf-8")
    rule = _rule(css, ".thinking.live .thinking-label")
    # The moving highlight is a background gradient kept animated.
    assert "background-image:" in rule
    assert "bui-shimmer-text" in rule
    # The gradient must be masked onto the glyphs and the fill made
    # transparent, otherwise it sits underneath solid text and cannot show.
    assert "background-clip: text;" in rule
    assert "-webkit-background-clip: text;" in rule
    assert "-webkit-text-fill-color: transparent;" in rule


def test_beautiful_thinking_shimmer_keyframes_exist():
    """The animation must actually be wired to a real @keyframes block so the
    moving background-position never has nothing to animate."""
    css = BEAUTIFUL_CSS.read_text(encoding="utf-8")
    frames = _rule(css, "@keyframes bui-shimmer-text")
    assert "background-position" in frames
    assert "from" in frames
    assert "to" in frames


def test_beautiful_thinking_shimmer_respects_reduced_motion():
    """With the fill now transparent, a user who asked the OS to stop moving
    things must get the solid label back instead of a frozen gradient."""
    css = BEAUTIFUL_CSS.read_text(encoding="utf-8")
    block = _rule(css, "@media (prefers-reduced-motion: reduce)")
    assert ".thinking.live .thinking-label" in block
    assert "-webkit-text-fill-color: currentColor;" in block
    assert "animation: none" in block
    assert "background-clip: border-box;" in block


def test_base_sheet_still_exposes_the_same_recipe_for_parity():
    """The base code.css (loaded before the beautiful sheet) already had the
    correct clip; keep both in sync so the recipe can't silently regress."""
    css = BASE_CSS.read_text(encoding="utf-8")
    rule = _rule(css, ".thinking.live .thinking-label")
    assert "background-clip: text;" in rule
    assert "-webkit-text-fill-color: transparent;" in rule