import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("AGENT_MODEL", "gpt-5.5")
MAX_ROUNDS = int(os.getenv("AGENT_MAX_ROUNDS", "8"))
OCR_BACKEND = os.getenv("AGENT_OCR", "easyocr")
OMNIPARSER_PATH = os.getenv("OMNIPARSER_PATH", "").strip()

# Optional SAM3 integration. Leave blank unless installed locally.
SAM3_PYTHON = os.getenv("SAM3_PYTHON", "").strip()
SAM3_SCRIPT = os.getenv("SAM3_SCRIPT", "").strip()
SAM3_CHECKPOINT = os.getenv("SAM3_CHECKPOINT", "").strip()

# Max pixels sent to VLM (downscale to keep cost/latency tolerable).
# Coordinates are always returned in ORIGINAL image space.
VLM_MAX_DIM = 1600
