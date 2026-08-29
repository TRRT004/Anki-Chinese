import requests
import logging
from config import NOTION_TOKEN, DATABASE_ID, NOTION_VERSION

log = logging.getLogger(__name__)

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

def _rich_text(prop: dict | None) -> str:
	if not prop:
		return ""
	return "".join(t["plain_text"] for t in prop.get("rich_text", []))

def _title(prop: dict | None) -> str:
	if not prop:
		return ""
	return "".join(t["plain_text"] for t in prop.get("title", []))

def _select(prop: dict | None) -> str:
	if not prop:
		return ""
	sel = prop.get("select")
	return sel["name"] if sel else ""

def _checkbox(prop: dict | None) -> bool:
	if not prop:
		return False
	return bool(prop.get("checkbox", False))

def _multi_select(prop: dict | None) -> list[str]:
	if not prop:
		return []
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

	# Check for Exclude checkbox
	exclude = "Exclude" in props and _checkbox(props["Exclude"])

	# Check for Conversation checkbox
	conversation = "Conversation" in props and _checkbox(props["Conversation"])

	# Skip rows where Ready checkbox exists but is unchecked, except if it is excluded (which we want to process for removal)
	if not exclude and "Ready" in props and not _checkbox(props["Ready"]):
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
		"exclude": exclude,
		"conversation": conversation,
	}
