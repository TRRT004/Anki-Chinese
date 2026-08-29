import hashlib
from pathlib import Path
import genanki
from config import (
	MODEL_ID, MODEL_NAME, AUDIO_PLACEMENT, DECK_ID, DECK_NAME,
	DECK_ID_NORMAL, DECK_NAME_NORMAL, DECK_ID_CONV, DECK_NAME_CONV
)

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
.tone1     { color: #ff4d4f; font-weight: bold; }
.tone2     { color: #52c41a; font-weight: bold; }
.tone3     { color: #1890ff; font-weight: bold; }
.tone4     { color: #9254de; font-weight: bold; }
.tone5     { color: #8c8c8c; }
"""

_COLORIZER_SCRIPT_PATH = Path(__file__).parent / "colorizer.js"
_COLORIZER_SCRIPT = f"<script>\n{_COLORIZER_SCRIPT_PATH.read_text(encoding='utf-8')}\n</script>"

def _make_model() -> genanki.Model:
	audio_front = "{{Audio}}" if AUDIO_PLACEMENT in ("front", "both") else ""
	audio_back = "{{Audio}}" if AUDIO_PLACEMENT in ("back", "both") else ""
	audio_back_production = "{{Audio}}" if AUDIO_PLACEMENT in ("front", "back", "both") else ""

	return genanki.Model(
		MODEL_ID,
		MODEL_NAME,
		fields=[
			{"name": "Chinese"},
			{"name": "Pinyin"},
			{"name": "Meaning"},
			{"name": "Type"},
			{"name": "Notes"},
			{"name": "Audio"},
		],
		templates=[
			{
				"name": "Recognition",
				"qfmt": f"<div class='chinese'>{{{{Chinese}}}}</div>{audio_front}",
				"afmt": (
					"{{FrontSide}}<hr>"
					"<div class='pinyin'>{{Pinyin}}</div>"
					"<div class='meaning'>{{Meaning}}</div>"
					"<div class='type'>{{Type}}</div>"
					"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
					f"{audio_back}"
					f"{_COLORIZER_SCRIPT}"
				),
			},
			{
				"name": "Production",
				"qfmt": "{{#Meaning}}<div class='meaning'>{{Meaning}}</div>{{/Meaning}}",
				"afmt": (
					"{{FrontSide}}<hr>"
					"<div class='chinese'>{{Chinese}}</div>"
					"<div class='pinyin'>{{Pinyin}}</div>"
					"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
					f"{audio_back_production}"
					f"{_COLORIZER_SCRIPT}"
				),
			},
		],
		css=_CSS,
	)

def _stable_guid(notion_id: str) -> str:
	return genanki.guid_for(notion_id)

def build_deck(rows: list[dict]) -> list[genanki.Deck]:
	"""Build normal and conversation genanki.Decks from Notion parsed rows."""
	deck_normal = genanki.Deck(DECK_ID_NORMAL, DECK_NAME_NORMAL)
	deck_conv = genanki.Deck(DECK_ID_CONV, DECK_NAME_CONV)
	model = _make_model()

	for row in rows:
		if row.get("exclude"):
			continue
		fields = [
			row["chinese"],
			row["pinyin"],
			row["meaning"],
			row["type"],
			row["notes"],
			"",
		]
		note = genanki.Note(
			model=model,
			fields=fields,
			guid=_stable_guid(row["id"]),
			tags=row["tags"],
		)
		if row.get("conversation"):
			deck_conv.add_note(note)
		else:
			deck_normal.add_note(note)

	return [deck_normal, deck_conv]
