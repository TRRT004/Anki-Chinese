import sys
import time
import logging
import asyncio
from pathlib import Path
import edge_tts
from config import (
	ANKIWEB_USERNAME, ANKIWEB_PASSWORD, ANKI_COLLECTION_PATH,
	AUDIO_PLACEMENT, MODEL_NAME, MODEL_ID, DECK_NAME
)
from anki_deck import _stable_guid, _make_model, _CSS, _COLORIZER_SCRIPT

log = logging.getLogger(__name__)

def upload_ankiweb(file_path: Path, filename: str, rows: list[dict]) -> str | None:
	"""
	Sync down from AnkiWeb, append/update Notion rows directly to the persistent local collection,
	and sync back up to AnkiWeb. Avoids importing .apkg which triggers full-sync/schema change prompts.
	"""
	import anki.lang
	from anki.collection import Collection
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

	def _wait_for_media_sync(col):
		log.info("Waiting for media sync to finish...")
		while True:
			status = col._backend.media_sync_status()
			if not status.active:
				break
			progress_str = str(status.progress).strip().replace("\n", " | ")
			if progress_str:
				log.info("Media sync progress: %s", progress_str)
			time.sleep(0.5)

	try:
		auth = col.sync_login(
			username=ANKIWEB_USERNAME,
			password=ANKIWEB_PASSWORD,
			endpoint=None,
		)

		log.info("Syncing down from AnkiWeb…")
		col.sync_collection(auth=auth, sync_media=True)
		_wait_for_media_sync(col)

		log.info("Updating local collection model schema…")
		model = col.models.by_name(MODEL_NAME)
		
		audio_front = "{{Audio}}" if AUDIO_PLACEMENT in ("front", "both") else ""
		audio_back = "{{Audio}}" if AUDIO_PLACEMENT in ("back", "both") else ""
		audio_back_production = "{{Audio}}" if AUDIO_PLACEMENT in ("front", "back", "both") else ""

		if not model:
			log.info("Creating new model in local collection…")
			model = col.models.new(MODEL_NAME)
			model["id"] = MODEL_ID
			
			for field_name in ["Chinese", "Pinyin", "Meaning", "Type", "Notes", "Audio"]:
				fld = col.models.new_field(field_name)
				col.models.add_field(model, fld)

			t1 = col.models.new_template("Recognition")
			t1["qfmt"] = f"<div class='chinese'>{{{{Chinese}}}}</div>{audio_front}"
			t1["afmt"] = (
				"{{FrontSide}}<hr>"
				"<div class='pinyin'>{{Pinyin}}</div>"
				"<div class='meaning'>{{Meaning}}</div>"
				"<div class='type'>{{Type}}</div>"
				"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
				f"{audio_back}"
				f"{_COLORIZER_SCRIPT}"
			)
			col.models.add_template(model, t1)

			t2 = col.models.new_template("Production")
			t2["qfmt"] = "{{#Meaning}}<div class='meaning'>{{Meaning}}</div>{{/Meaning}}"
			t2["afmt"] = (
				"{{FrontSide}}<hr>"
				"<div class='chinese'>{{Chinese}}</div>"
				"<div class='pinyin'>{{Pinyin}}</div>"
				"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
				f"{audio_back_production}"
				f"{_COLORIZER_SCRIPT}"
			)
			col.models.add_template(model, t2)
			
			model["css"] = _CSS
			col.models.add(model)
			model = col.models.by_name(MODEL_NAME)
		else:
			# Check if "Audio" field is in model
			field_names = [f["name"] for f in model["flds"]]
			if "Audio" not in field_names:
				log.info("Updating existing model to add Audio field...")
				fld = col.models.new_field("Audio")
				col.models.add_field(model, fld)
				
			# Unconditionally sync templates and CSS with tone colorizer
			log.info("Updating existing model templates and CSS rules…")
			t1 = model["tmpls"][0]
			t1["qfmt"] = f"<div class='chinese'>{{{{Chinese}}}}</div>{audio_front}"
			t1["afmt"] = (
				"{{FrontSide}}<hr>"
				"<div class='pinyin'>{{Pinyin}}</div>"
				"<div class='meaning'>{{Meaning}}</div>"
				"<div class='type'>{{Type}}</div>"
				"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
				f"{audio_back}"
				f"{_COLORIZER_SCRIPT}"
			)
			
			t2 = model["tmpls"][1]
			t2["qfmt"] = "{{#Meaning}}<div class='meaning'>{{Meaning}}</div>{{/Meaning}}"
			t2["afmt"] = (
				"{{FrontSide}}<hr>"
				"<div class='chinese'>{{Chinese}}</div>"
				"<div class='pinyin'>{{Pinyin}}</div>"
				"{{#Notes}}<div class='notes'>{{Notes}}</div>{{/Notes}}"
				f"{audio_back_production}"
				f"{_COLORIZER_SCRIPT}"
			)
			model["css"] = _CSS
			col.models.save(model)

		# Ensure deck exists
		deck_id = col.decks.id(DECK_NAME)

		# Ensure media folder exists
		media_dir = col_dir / "collection.media"
		media_dir.mkdir(parents=True, exist_ok=True)

		async def tts_download(text, path):
			communicate = edge_tts.Communicate(text, "zh-CN-YunyangNeural")
			await communicate.save(path)

		log.info("Adding/updating %d notes directly in collection…", len(rows))
		added_count = 0
		updated_count = 0
		suspended_count = 0
		unsuspended_count = 0
		untouched_count = 0
		for row in rows:
			guid = _stable_guid(row["id"])
			
			if row.get("exclude"):
				note_ids = col.db.list("select id from notes where guid = ?", guid)
				if not note_ids:
					note_ids = col.db.list(
						"select id from notes where flds = ? or flds like ?",
						row["chinese"],
						row["chinese"] + "\x1f%"
					)
				if note_ids:
					# Check if there are cards of these notes that are not currently suspended (queue != -1)
					unsuspended_card_ids = col.db.list(
						f"select id from cards where nid in ({','.join(['?'] * len(note_ids))}) and queue != -1",
						*note_ids
					)
					if unsuspended_card_ids:
						log.info("Suspending excluded note '%s' (guid: %s, ids: %s)", row["chinese"], guid, note_ids)
						try:
							col.sched.suspend_notes(note_ids)
							suspended_count += len(note_ids)
						except Exception as susp_exc:
							log.error("Failed to suspend note '%s': %s", row["chinese"], susp_exc)
				continue

			audio_filename = f"zh_{row['id']}.mp3"
			audio_path = media_dir / audio_filename

			if not col.media.have(audio_filename):
				if audio_path.exists():
					log.info("Registering existing audio file in database: %s", audio_filename)
					try:
						col.media.add_file(str(audio_path.resolve()))
					except Exception as add_exc:
						log.error("Failed to register existing audio: %s", add_exc)
				else:
					log.info("Generating audio for '%s' -> %s", row["chinese"], audio_filename)
					try:
						temp_path = Path("/tmp") / audio_filename
						asyncio.run(tts_download(row["chinese"], str(temp_path.resolve())))
						col.media.add_file(str(temp_path.resolve()))
						if temp_path.exists():
							temp_path.unlink()
					except Exception as tts_exc:
						log.error("Failed to generate TTS audio for '%s': %s", row["chinese"], tts_exc)

			note_ids = col.db.list("select id from notes where guid = ?", guid)
			if not note_ids:
				matching_ids = col.db.list(
					"select id from notes where flds = ? or flds like ?",
					row["chinese"],
					row["chinese"] + "\x1f%"
				)
				if matching_ids:
					note_ids = [matching_ids[0]]
					old_note = col.get_note(matching_ids[0])
					log.info("Migrating GUID of old note '%s': %s -> %s", row["chinese"], old_note.guid, guid)
					old_note.guid = guid
					col.update_note(old_note)
			
			was_unsuspended = False
			if note_ids:
				try:
					# Get only cards of this note that are currently suspended (queue = -1)
					suspended_card_ids = col.db.list(
						"select id from cards where nid = ? and queue = -1",
						note_ids[0]
					)
					if suspended_card_ids:
						log.info("Unsuspending note '%s' cards: %s", row["chinese"], suspended_card_ids)
						col.sched.unsuspend_cards(suspended_card_ids)
						unsuspended_count += len(suspended_card_ids)
						was_unsuspended = True
				except Exception as unsusp_exc:
					log.error("Failed to unsuspend note '%s': %s", row["chinese"], unsusp_exc)

			fields = [
				row["chinese"],
				row["pinyin"],
				row["meaning"],
				row["type"],
				row["notes"],
				f"[sound:{audio_filename}]",
			]
			
			if note_ids:
				note = col.get_note(note_ids[0])
				# Ensure fields array is padded to schema size
				while len(note.fields) < len(model["flds"]):
					note.fields.append("")
					
				changed = False
				for i, val in enumerate(fields):
					if note.fields[i] != val:
						note.fields[i] = val
						changed = True
				
				# Check tags
				new_tags = row["tags"]
				if sorted(note.tags) != sorted(new_tags):
					note.tags = new_tags
					changed = True
				
				if changed:
					col.update_note(note)
					updated_count += 1
				elif not was_unsuspended:
					untouched_count += 1
			else:
				note = col.new_note(model)
				note.guid = guid
				for i, val in enumerate(fields):
					note.fields[i] = val
				note.tags = row["tags"]
				col.add_note(note, deck_id)
				added_count += 1
		
		log.info("Finished notes sync. Added: %d, Updated: %d, Suspended: %d, Unsuspended: %d, Untouched: %d", added_count, updated_count, suspended_count, unsuspended_count, untouched_count)

		# Cleanup orphaned media files (zh_*.mp3 no longer in the active Notion rows)
		log.info("Checking for orphaned media files…")
		active_files = {f"zh_{row['id']}.mp3" for row in rows}
		orphaned_files = []
		for media_file in media_dir.glob("zh_*.mp3"):
			if media_file.name not in active_files:
				orphaned_files.append(media_file.name)
		
		if orphaned_files:
			log.info("Trashing %d orphaned media files from database...", len(orphaned_files))
			try:
				col.media.trash_files(orphaned_files)
			except Exception as trash_exc:
				log.error("Failed to trash orphaned media: %s", trash_exc)

		# Sync UP to AnkiWeb
		log.info("Syncing collection back to AnkiWeb…")
		sync_result = col.sync_collection(auth=auth, sync_media=True)
		_wait_for_media_sync(col)

		if sync_result.required == SyncOutput.NO_CHANGES:
			log.info("AnkiWeb already up to date — no changes to push")

		elif sync_result.required in (SyncOutput.FULL_SYNC, SyncOutput.FULL_UPLOAD):
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
			log.info("Normal sync complete")

		log.info("AnkiWeb account: %s — open Anki on any device and sync to receive updates", ANKIWEB_USERNAME)
		return f"ankiweb-{ANKIWEB_USERNAME}"
	finally:
		try:
			col.close()
		except Exception:
			pass

def get_anki_day_cutoff() -> int:
	"""
	Log in to AnkiWeb, sync down to ensure we have the latest collection settings,
	and return the next daily rollover/reset Unix timestamp (col.sched.day_cutoff).
	"""
	import anki.lang
	from anki.collection import Collection

	if not ANKIWEB_USERNAME or not ANKIWEB_PASSWORD:
		log.error("ANKIWEB_USERNAME and ANKIWEB_PASSWORD are required for checking cutoff.")
		sys.exit(1)

	anki.lang.set_lang("en")
	col_dir = Path(ANKI_COLLECTION_PATH)
	col_dir.mkdir(parents=True, exist_ok=True)
	col_path = str(col_dir / "collection.anki2")

	col = Collection(col_path)
	try:
		auth = col.sync_login(
			username=ANKIWEB_USERNAME,
			password=ANKIWEB_PASSWORD,
			endpoint=None,
		)
		from anki.sync import SyncOutput
		sync_result = col.sync_collection(auth=auth, sync_media=False)
		if sync_result.required in (SyncOutput.FULL_SYNC, SyncOutput.FULL_UPLOAD):
			if sync_result.new_endpoint:
				auth.endpoint = sync_result.new_endpoint
			col.close_for_full_sync()
			col.full_upload_or_download(
				auth=auth,
				server_usn=None,
				upload=False,
			)
			col.reopen(after_full_sync=True)
		
		return col.sched.day_cutoff
	finally:
		try:
			col.close()
		except Exception:
			pass
