"""aiOS desktop shell rendered by WebView2 instead of Tk.

The Python side of aiOS is unchanged: code_jobs, voice_agent, phone_relay and
friends are still the backend. Only the view layer moved off Tk, because Tk has
no retained scene graph -- every tab switch rebuilt the whole widget tree and
every streamed token relaid out a Text widget. A DOM keeps its own scene graph
and composites on the GPU, which is what makes CODE feel smooth.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

__all__ = ["BASE_DIR", "WEB_DIR"]
