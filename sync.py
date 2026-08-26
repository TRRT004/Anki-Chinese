import os
import sys
import time
import signal
import logging
from datetime import date, datetime
from pathlib import Path

from config import UPLOAD_BACKENDS
from notion import fetch_notion_pages, parse_row
from anki_deck import build_deck
from anki_sync import upload_ankiweb, get_anki_day_cutoff
from github import upload_github
import genanki

# Setup logging
logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s [%(levelname)s] %(message)s",
	handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

def upload_apkg(file_path: Path, filename: str, rows: list[dict]) -> list[str]:
	"""Upload the generated deck to all configured backends."""
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
		ankiweb_url = upload_ankiweb(file_path, filename, rows)
		if ankiweb_url:
			urls.append(ankiweb_url)
	return urls

def run_once() -> None:
	"""Orchestrate a single-run synchronization process."""
	log.info("=== Notion → Anki sync started ===")
	pages = fetch_notion_pages()
	
	rows = []
	for page in pages:
		row = parse_row(page)
		if row:
			rows.append(row)
			
	log.info("Parsed %d valid rows, skipped %d", len(rows), len(pages) - len(rows))
	
	deck = build_deck(rows)
	log.info("Deck built: %d notes", len(deck.notes))
	
	# Generate .apkg
	out_dir = Path(os.getenv("OUTPUT_DIR", "./output"))
	out_dir.mkdir(parents=True, exist_ok=True)
	
	today_str = date.today().isoformat()
	filename = f"ChineseVocab_{today_str}.apkg"
	out_path = out_dir / filename
	
	genanki.Package(deck).write_to_file(str(out_path.resolve()))
	log.info("Wrote deck: %s (%d bytes)", out_path, out_path.stat().st_size)
	
	urls = upload_apkg(out_path, filename, rows)
	for url in urls:
		log.info("Deck available at: %s", url)
		
	log.info("=== Sync complete ===")

def run_daemon() -> None:
	"""Run the synchronization process in a daemon loop."""
	log.info("Starting sync daemon...")
	
	# Handle signals
	def stop_handler(signum, frame):
		log.info("Received stop signal (%d). Exiting…", signum)
		sys.exit(0)
		
	signal.signal(signal.SIGTERM, stop_handler)
	signal.signal(signal.SIGINT, stop_handler)
	
	last_successful_sync_date = ""
	
	while True:
		try:
			day_cutoff = get_anki_day_cutoff()
		except Exception as exc:
			log.error("Failed to check AnkiWeb day cutoff: %s. Retrying in 15 minutes…", exc)
			time.sleep(900)
			continue

		now = int(time.time())
		# Sync 10 minutes before the daily reset window
		target_time = day_cutoff - 600

		try:
			from datetime import timezone
			target_dt_utc = datetime.fromtimestamp(target_time, timezone.utc)
		except ImportError:
			target_dt_utc = datetime.utcfromtimestamp(target_time)
		target_dt_local = datetime.fromtimestamp(target_time)

		today_str = date.today().isoformat()
		if now >= target_time:
			if last_successful_sync_date != today_str:
				log.info("Current time is within the daily reset window and today's sync has not completed yet. Running sync…")
				try:
					run_once()
					last_successful_sync_date = today_str
					log.info("Daily sync succeeded. Next sync scheduled for tomorrow's window.")
					# Sleep 15 minutes to prevent hot looping in the same window
					time.sleep(900)
					continue
				except Exception as exc:
					log.error("Daily sync failed: %s. Retrying in 15 minutes…", exc)
					time.sleep(900)
					continue
			else:
				# Already synced successfully today, sleep until the next day cutoff check
				log.info("Already synced successfully today (%s). Checking again in 4 hours...", today_str)
				time.sleep(4 * 3600)
				continue

		# Sleep until the target time, but check every 4 hours to verify if the setting has changed on AnkiWeb
		sleep_seconds = min(target_time - now, 4 * 3600)
		log.info("Sleeping for %d seconds (Next sync target: %s UTC / %s Local)", 
				 sleep_seconds, target_dt_utc.isoformat(), target_dt_local.isoformat())
		time.sleep(sleep_seconds)

def main() -> None:
	if os.getenv("RUN_AS_DAEMON", "").lower() == "true":
		run_daemon()
	else:
		run_once()

if __name__ == "__main__":
	main()
