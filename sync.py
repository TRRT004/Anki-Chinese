"""
Notion → Anki sync service.

Fetches vocabulary from a Notion database, builds a genanki .apkg deck,
and uploads it to a cloud backend (S3 or GitHub Releases).
"""

import hashlib
import logging
import os
import sys
from datetime import date
from pathlib import Path

import genanki
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s [%(levelname)s] %(message)s",
	handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

def _require(var: str) -> str:
	val = os.getenv(var)
	if not val:
		log.error("Required environment variable %s is not set.", var)
		sys.exit(1)
	return val


NOTION_TOKEN    = _require("NOTION_TOKEN")
DATABASE_ID     = _require("DATABASE_ID")
# Comma-separated list: github, ankiweb, none  (e.g. "github,ankiweb" uploads to both)
UPLOAD_BACKENDS = {b.strip() for b in os.getenv("UPLOAD_BACKEND", "none").lower().split(",") if b.strip()}

# GitHub backend
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO     = os.getenv("GITHUB_REPO", "")   # format: owner/repo

# AnkiWeb backend — syncs directly to AnkiWeb from the server, no Anki desktop required.
# Uses the official `anki` Python package (no Qt, no GUI).
# A persistent collection is stored in ANKI_COLLECTION_PATH (mounted Docker volume).
ANKIWEB_USERNAME        = os.getenv("ANKIWEB_USERNAME", "")
ANKIWEB_PASSWORD        = os.getenv("ANKIWEB_PASSWORD", "")
ANKI_COLLECTION_PATH    = os.getenv("ANKI_COLLECTION_PATH", "./collection")

NOTION_VERSION  = "2022-06-28"
DECK_NAME       = "ChineseVocab"
MODEL_NAME      = "ChineseVocabModel"

# Stable integer IDs derived from names so re-runs never create duplicates
MODEL_ID = int(hashlib.md5(MODEL_NAME.encode()).hexdigest()[:8], 16)
DECK_ID  = int(hashlib.md5(DECK_NAME.encode()).hexdigest()[:8], 16)

# ── Notion API ─────────────────────────────────────────────────────────────────

def _headers() -> dict:
	return {
		"Authorization": f"Bearer {NOTION_TOKEN}",
		"Notion-Version": NOTION_VERSION,
		"Content-Type": "application/json",
	}


def fetch_notion_pages() -> list[dict]:
	"""Fetch every page from the database, handling Notion's 100-item pagination."""
	url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
	pages: list[dict] = []
	cursor: str | None = None

	while True:
		payload: dict = {"page_size": 100}
		if cursor:
			payload["start_cursor"] = cursor

		resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
		resp.raise_for_status()
		data = resp.json()

		batch = data.get("results", [])
		pages.extend(batch)
		log.info("Fetched %d pages so far…", len(pages))

		if not data.get("has_more"):
			break
		cursor = data["next_cursor"]

	log.info("Notion fetch complete: %d total pages", len(pages))
	return pages


# ── Notion field extractors ────────────────────────────────────────────────────

def _rich_text(prop: dict) -> str:
	return "".join(t["plain_text"] for t in prop.get("rich_text", []))


def _title(prop: dict) -> str:
	return "".join(t["plain_text"] for t in prop.get("title", []))


def _select(prop: dict) -> str:
	sel = prop.get("select")
	return sel["name"] if sel else ""


def _checkbox(prop: dict) -> bool:
	return bool(prop.get("checkbox", False))


def _multi_select(prop: dict) -> list[str]:
	return [o["name"] for o in prop.get("multi_select", [])]


def parse_row(page: dict) -> dict | None:
	"""
	Extract vocabulary fields from a Notion page.
	Returns None for rows that should be skipped.
	"""
	props = page.get("properties", {})

	# Chinese word — support both common field names
	chinese_prop = props.get("Word") or props.get("Chinese") or {}
	if chinese_prop.get("type") == "title":
		chinese = _title(chinese_prop)
	else:
		chinese = _rich_text(chinese_prop)

	chinese = chinese.strip()
	if not chinese:
		log.debug("Skipping page %s: empty word field", page["id"])
		return None

	# Skip rows where Ready checkbox exists but is unchecked
	if "Ready" in props and not _checkbox(props["Ready"]):
		log.debug("Skipping '%s': Ready=false", chinese)
		return None

	# Meaning — support both common field names
	meaning_prop = props.get("Translation") or props.get("Meaning") or {}

	return {
		"id":      page["id"],
		"chinese": chinese,
		"pinyin":  _rich_text(props.get("Pinyin",  {})).strip(),
		"meaning": _rich_text(meaning_prop).strip(),
		"type":    _select(   props.get("Type",    {})).strip(),
		"notes":   _rich_text(props.get("Notes",   {})).strip(),
		"tags":    _multi_select(props.get("Tags", {})),
	}


# ── Anki model & deck ──────────────────────────────────────────────────────────

_CSS = """
.card      { font-family: Arial, sans-serif; font-size: 20px; text-align: center;
			 background: #fafafa; color: #1a1a1a; padding: 20px; }
.chinese   { font-size: 52px; font-weight: bold; margin-bottom: 8px; }
.pinyin    { font-size: 26px; color: #4a6cf7; margin: 6px 0; }
.meaning   { font-size: 22px; margin: 4px 0; }
.type      { font-size: 15px; color: #888; margin-top: 8px; }
.notes     { font-size: 15px; color: #666; font-style: italic; margin-top: 10px;
			 border-top: 1px solid #ddd; padding-top: 8px; }
hr         { border: none; border-top: 1px solid #ddd; margin: 14px 0; }
"""

def _make_model() -> genanki.Model:
	return genanki.Model(
		MODEL_ID,
		MODEL_NAME,
		fields=[
			{"name": "Chinese"},
			{"name": "Pinyin"},
			{"name": "Meaning"},
			{"name": "Type"},
			{"name": "Notes"},
		],
		templates=[
			{
				# Recognition: see the character → recall meaning
				"name": "Recognition",
				"qfmt": "<div class='chinese'>{{Chinese}}</div>",
				"afmt": (
					"{{FrontSide}}<hr>"
					"<div class='pinyin'>{{Pinyin}}</div>"
					"<div class='meaning'>{{Meaning}}</div>"
					"<div class='type'>{{Type}}</div>"
					"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
				),
			},
			{
				# Production: see the meaning → recall the character
				"name": "Production",
				"qfmt": "<div class='meaning'>{{Meaning}}</div>",
				"afmt": (
					"{{FrontSide}}<hr>"
					"<div class='chinese'>{{Chinese}}</div>"
					"<div class='pinyin'>{{Pinyin}}</div>"
					"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
				),
			},
		],
		css=_CSS,
	)


def _stable_guid(notion_id: str) -> int:
	"""Deterministic integer GUID from a Notion page ID (ensures idempotent updates)."""
	return int(hashlib.md5(notion_id.encode()).hexdigest()[:8], 16)


def build_deck(rows: list[dict]) -> genanki.Deck:
	model = _make_model()
	deck  = genanki.Deck(DECK_ID, DECK_NAME)

	for row in rows:
		note = genanki.Note(
			model=model,
			fields=[
				row["chinese"],
				row["pinyin"],
				row["meaning"],
				row["type"],
				row["notes"],
			],
			tags=row["tags"],
			guid=_stable_guid(row["id"]),
		)
		deck.add_note(note)
		log.debug("Added note: %s", row["chinese"])

	log.info("Deck built: %d notes", len(deck.notes))
	return deck


# ── Upload backends ────────────────────────────────────────────────────────────

def upload_ankiweb(file_path: Path, filename: str) -> str:
	"""
	Import the .apkg into a persistent local collection, then sync it to AnkiWeb.
	Uses the official `anki` Python package — no Anki desktop or Qt required.
	The collection is stored in ANKI_COLLECTION_PATH (a Docker volume) so state
	is preserved across runs and only diffs are sent on subsequent syncs.
	"""
	import anki.lang
	from anki.collection import Collection, ImportAnkiPackageRequest
	from anki.sync import SyncOutput

	if not ANKIWEB_USERNAME or not ANKIWEB_PASSWORD:
		log.error("ANKIWEB_USERNAME and ANKIWEB_PASSWORD are required for ankiweb backend.")
		sys.exit(1)

	# i18n must be initialised before Collection is opened
	anki.lang.set_lang("en")

	col_dir = Path(ANKI_COLLECTION_PATH)
	col_dir.mkdir(parents=True, exist_ok=True)
	col_path = str(col_dir / "collection.anki2")

	log.info("Opening collection at %s", col_path)
	col = Collection(col_path)
	try:
		# Import via the modern API — updates existing notes via stable GUIDs, no duplicates
		log.info("Importing %s into collection…", filename)
		col.import_anki_package(
			ImportAnkiPackageRequest(package_path=str(file_path.resolve()))
		)
		log.info("Import done")

		# Login to AnkiWeb
		log.info("Logging in to AnkiWeb as %s…", ANKIWEB_USERNAME)
		auth = col.sync_login(
			username=ANKIWEB_USERNAME,
			password=ANKIWEB_PASSWORD,
			endpoint=None,
		)

		# Sync — handle all three outcomes AnkiWeb can return
		log.info("Syncing collection to AnkiWeb…")
		sync_result = col.sync_collection(auth=auth, sync_media=False)

		if sync_result.required == SyncOutput.NO_CHANGES:
			log.info("AnkiWeb already up to date — no changes to push")

		elif sync_result.required in (SyncOutput.FULL_SYNC, SyncOutput.FULL_UPLOAD):
			# First sync or AnkiWeb explicitly requesting upload: send entire collection.
			# AnkiWeb routes accounts to specific servers — use the endpoint it returned.
			if sync_result.new_endpoint:
				auth.endpoint = sync_result.new_endpoint
				log.info("Using sync endpoint: %s", sync_result.new_endpoint)
			log.info("Full upload required — sending entire collection to AnkiWeb…")
			col.close_for_full_sync()
			col.full_upload_or_download(
				auth=auth,
				server_usn=None,  # None = collection only, skip media
				upload=True,
			)
			log.info("Full upload complete")

		else:
			# NORMAL_SYNC — incremental diff already applied by sync_collection
			log.info("Normal sync complete")

		log.info("AnkiWeb account: %s — open Anki on any device and sync to receive updates", ANKIWEB_USERNAME)
	finally:
		# close() is safe to call even if close_for_full_sync() already ran
		try:
			col.close()
		except Exception:
			pass

	return None  # no download URL — devices pull from AnkiWeb on their next sync


def upload_github(file_path: Path, filename: str) -> str:
	"""
	Create (or reuse) a dated GitHub release and attach the .apkg as an asset.
	Idempotent: replaces any existing asset with the same filename.
	"""
	if not GITHUB_TOKEN or not GITHUB_REPO:
		log.error("GITHUB_TOKEN and GITHUB_REPO are required for GitHub backend.")
		sys.exit(1)

	tag     = f"anki-{date.today().isoformat()}"
	api     = f"https://api.github.com/repos/{GITHUB_REPO}"
	headers = {
		"Authorization": f"token {GITHUB_TOKEN}",
		"Accept":        "application/vnd.github+json",
		"X-GitHub-Api-Version": "2022-11-28",
	}

	# Reuse existing release for today or create a new one
	resp = requests.get(f"{api}/releases/tags/{tag}", headers=headers, timeout=15)
	if resp.status_code == 200:
		release = resp.json()
		log.info("Reusing existing GitHub release: %s", tag)
	else:
		payload = {
			"tag_name":         tag,
			"name":             f"Anki deck {date.today().isoformat()}",
			"body":             "Auto-generated by anki-notion-sync",
			"draft":            False,
			"prerelease":       False,
		}
		resp = requests.post(f"{api}/releases", headers=headers, json=payload, timeout=15)
		resp.raise_for_status()
		release = resp.json()
		log.info("Created GitHub release: %s", tag)

	# Delete stale asset with the same name (so re-upload is clean)
	for asset in release.get("assets", []):
		if asset["name"] == filename:
			del_resp = requests.delete(
				f"{api}/releases/assets/{asset['id']}", headers=headers, timeout=15
			)
			del_resp.raise_for_status()
			log.info("Removed stale asset: %s", filename)

	# Upload the new .apkg
	upload_url = release["upload_url"].split("{")[0]  # strip URI template suffix
	with file_path.open("rb") as fh:
		up_resp = requests.post(
			upload_url,
			headers={**headers, "Content-Type": "application/octet-stream"},
			params={"name": filename},
			data=fh,
			timeout=120,
		)
	up_resp.raise_for_status()
	url = up_resp.json()["browser_download_url"]
	log.info("GitHub upload complete: %s", url)
	return url


def upload_apkg(file_path: Path, filename: str) -> list[str | None]:
	"""Upload to every enabled backend. Returns list of URLs."""
	active = UPLOAD_BACKENDS - {"none"}
	if not active:
		log.info("UPLOAD_BACKEND=none — skipping upload. File at: %s", file_path)
		return []

	unknown = active - {"github", "ankiweb"}
	if unknown:
		log.error("Unknown backend(s): %s. Valid values: github, ankiweb, none", unknown)
		sys.exit(1)

	urls = []
	if "github" in active:
		urls.append(upload_github(file_path, filename))
	if "ankiweb" in active:
		urls.append(upload_ankiweb(file_path, filename))
	return urls


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
	log.info("=== Notion → Anki sync started ===")

	# 1. Fetch all pages from Notion
	pages = fetch_notion_pages()

	# 2. Parse & filter rows
	rows = [row for page in pages if (row := parse_row(page)) is not None]
	skipped = len(pages) - len(rows)
	log.info("Parsed %d valid rows, skipped %d", len(rows), skipped)

	if not rows:
		log.warning("No valid rows found. Nothing to sync.")
		sys.exit(0)

	# 3. Build Anki deck
	deck = build_deck(rows)

	# 4. Write .apkg file
	filename = f"ChineseVocab_{date.today().isoformat()}.apkg"
	out_dir = Path(os.getenv("OUTPUT_DIR", "/output"))
	out_path = out_dir / filename
	out_path.parent.mkdir(parents=True, exist_ok=True)

	genanki.Package(deck).write_to_file(str(out_path))
	log.info("Wrote deck: %s (%d bytes)", out_path, out_path.stat().st_size)

	# 5. Upload to cloud
	try:
		urls = upload_apkg(out_path, filename)
		for url in urls:
			if url:
				log.info("Deck available at: %s", url)
	except Exception as exc:
		log.error("Upload failed: %s", exc, exc_info=True)
		sys.exit(1)

	log.info("=== Sync complete ===")


if __name__ == "__main__":
	main()
