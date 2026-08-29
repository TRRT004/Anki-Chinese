import os
import sys
import logging
import hashlib
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

def _require(var: str) -> str:
	val = os.getenv(var)
	if not val:
		log.error("Required environment variable %s is not set.", var)
		sys.exit(1)
	return val

NOTION_TOKEN    = _require("NOTION_TOKEN")
DATABASE_ID     = _require("DATABASE_ID")
UPLOAD_BACKENDS = {b.strip() for b in os.getenv("UPLOAD_BACKEND", "none").lower().split(",") if b.strip()}

GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO     = os.getenv("GITHUB_REPO", "")

ANKIWEB_USERNAME        = os.getenv("ANKIWEB_USERNAME", "")
ANKIWEB_PASSWORD        = os.getenv("ANKIWEB_PASSWORD", "")
ANKI_COLLECTION_PATH    = os.getenv("ANKI_COLLECTION_PATH", "./collection")

NOTION_VERSION  = "2022-06-28"
DECK_NAME       = "ChineseVocab"
DECK_NAME_NORMAL = f"{DECK_NAME}::Normal"
DECK_NAME_CONV   = f"{DECK_NAME}::Conversation"
MODEL_NAME      = "ChineseVocabModel"
AUDIO_PLACEMENT = os.getenv("AUDIO_PLACEMENT", "both").lower()
RUN_AS_DAEMON   = os.getenv("RUN_AS_DAEMON", "false").lower() == "true"
OUTPUT_DIR      = os.getenv("OUTPUT_DIR", "./output")

MODEL_ID = int(hashlib.md5(MODEL_NAME.encode()).hexdigest()[:8], 16)
DECK_ID  = int(hashlib.md5(DECK_NAME.encode()).hexdigest()[:8], 16)
DECK_ID_NORMAL = int(hashlib.md5(DECK_NAME_NORMAL.encode()).hexdigest()[:8], 16)
DECK_ID_CONV   = int(hashlib.md5(DECK_NAME_CONV.encode()).hexdigest()[:8], 16)
