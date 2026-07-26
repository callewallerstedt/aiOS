"""The phone and the PC must agree on which files an attachment can be.

The picker lives in the PWA and the decoder lives in the Flask app, so nothing
but a test stops one side from accepting a file the other side throws away.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "phone_site" / "index.html"
PHONE_JS = ROOT / "phone_site" / "phone.js"
SERVER_PY = ROOT / "agent_clicker" / "app" / "server.py"


def module_set(path, name):
    """Read a module-level set literal without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} is gone from {path.name}")


def picker_extensions():
    accept = re.search(r'id="fileInput"[^>]*accept="([^"]+)"', INDEX_HTML.read_text(encoding="utf-8"), re.S)
    assert accept, "the composer lost its file picker"
    return {value.strip().lower() for value in accept.group(1).split(",") if value.strip().startswith(".")}


def js_constant(name):
    match = re.search(rf"^const {name} = (.+?);\s*(?://.*)?$", PHONE_JS.read_text(encoding="utf-8"), re.M)
    assert match, f"{name} is gone from phone.js"
    return match.group(1)


def test_every_offered_file_type_can_be_saved_on_the_pc():
    accepted = module_set(SERVER_PY, "_TEXT_UPLOAD_EXTS") | module_set(SERVER_PY, "_UPLOAD_EXTS")

    unusable = picker_extensions() - accepted
    assert not unusable, f"the phone offers files aiOS cannot store: {sorted(unusable)}"


def test_the_picker_also_offers_the_camera_roll():
    accept = re.search(r'id="fileInput"[^>]*accept="([^"]+)"', INDEX_HTML.read_text(encoding="utf-8"), re.S)
    assert "image/*" in accept.group(1), "photos must stay one tap away"


def test_dropped_text_files_match_what_the_picker_offers():
    # A dropped or pasted file arrives without a useful MIME type, so phone.js
    # sniffs it by extension. That list must not drift from the picker's own.
    source = js_constant("TEXT_FILE_PATTERN").strip().strip("/i").strip("/")
    sniffs = re.compile(source, re.I)   # the pattern is plain enough for both engines
    offered = {ext for ext in picker_extensions() if ext not in module_set(SERVER_PY, "_UPLOAD_EXTS")}

    rejected = sorted(ext for ext in offered if not sniffs.search(f"note{ext}"))
    assert not rejected, f"the picker offers files a drop would reject: {rejected}"


def test_the_relay_and_the_phone_agree_on_how_many_files_travel():
    import phone_relay

    assert int(js_constant("MAX_ATTACHMENTS")) == phone_relay.MAX_ATTACHMENTS


def test_the_relay_and_the_phone_agree_on_the_size_ceiling():
    js_bytes = eval(js_constant("MAX_FILE_BYTES"))  # noqa: S307 - a literal from our own source

    import phone_relay

    assert js_bytes == phone_relay.MAX_ATTACHMENT_BYTES
