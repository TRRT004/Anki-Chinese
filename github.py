import logging
from pathlib import Path
import requests
from config import GITHUB_TOKEN, GITHUB_REPO

log = logging.getLogger(__name__)

def upload_github(file_path: Path, filename: str) -> str:
	"""Upload the generated deck file to GitHub Releases."""
	if not GITHUB_TOKEN or not GITHUB_REPO:
		log.error("GITHUB_TOKEN and GITHUB_REPO are required for github backend.")
		import sys
		sys.exit(1)

	headers = {
		"Authorization": f"token {GITHUB_TOKEN}",
		"Accept": "application/vnd.github.v3+json",
	}

	# Format date for tag (e.g. anki-2023-10-27)
	from datetime import date
	tag_name = f"anki-{date.today().isoformat()}"

	log.info("Releasing to GitHub repository %s with tag %s…", GITHUB_REPO, tag_name)

	# 1. Check if release already exists
	release_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag_name}"
	resp = requests.get(release_url, headers=headers, timeout=15)
	
	release_id = None
	if resp.status_code == 200:
		release_data = resp.json()
		release_id = release_data["id"]
		log.info("Reusing existing GitHub release: %s", tag_name)
		
		# Remove existing asset if same name
		for asset in release_data.get("assets", []):
			if asset["name"] == filename:
				log.info("Removing stale asset: %s", filename)
				requests.delete(asset["url"], headers=headers, timeout=15).raise_for_status()
	
	elif resp.status_code == 404:
		# Create release
		create_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
		payload = {
			"tag_name": tag_name,
			"name": f"Anki Chinese Vocab Sync {date.today().isoformat()}",
			"body": "Automated daily sync of Notion vocabulary to Anki",
			"draft": False,
			"prerelease": False,
		}
		create_resp = requests.post(create_url, headers=headers, json=payload, timeout=15)
		create_resp.raise_for_status()
		release_id = create_resp.json()["id"]
		log.info("Created new GitHub release: %s", tag_name)
	else:
		resp.raise_for_status()

	# 2. Upload asset
	upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets?name={filename}"
	headers["Content-Type"] = "application/octet-stream"
	
	with open(file_path, "rb") as f:
		upload_resp = requests.post(upload_url, headers=headers, data=f, timeout=60)
	
	upload_resp.raise_for_status()
	browser_download_url = upload_resp.json()["browser_download_url"]
	log.info("GitHub upload complete: %s", browser_download_url)
	return browser_download_url
