"""
Notion → Anki sync service.

Fetches vocabulary from a Notion database, builds a genanki .apkg deck,
and uploads it to a cloud backend (S3 or GitHub Releases).
"""

import asyncio
import hashlib
import logging
import os
import signal
import sys
import time
from datetime import date, datetime
from pathlib import Path
import edge_tts

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
AUDIO_PLACEMENT = os.getenv("AUDIO_PLACEMENT", "both").lower()  # front, back, both

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
.tone1     { color: #ff4d4f; font-weight: bold; }
.tone2     { color: #52c41a; font-weight: bold; }
.tone3     { color: #1890ff; font-weight: bold; }
.tone4     { color: #9254de; font-weight: bold; }
.tone5     { color: #8c8c8c; }
"""

_COLORIZER_SCRIPT = """
<script>
(function(){
var pEls=document.querySelectorAll('.pinyin');
if(!pEls.length)return;
var chEl=document.querySelector('.chinese');

/* Tone detection: char → tone number */
var TM={};
'āēīōūǖ'.split('').forEach(function(c){TM[c]=1;});
'áéíóúǘ'.split('').forEach(function(c){TM[c]=2;});
'ǎěǐǒǔǚ'.split('').forEach(function(c){TM[c]=3;});
'àèìòùǜ'.split('').forEach(function(c){TM[c]=4;});

/* Normalize tone-marked vowels → base vowels for syllable lookup */
var NM={};
'āáǎà'.split('').forEach(function(c){NM[c]='a';});
'ēéěè'.split('').forEach(function(c){NM[c]='e';});
'īíǐì'.split('').forEach(function(c){NM[c]='i';});
'ōóǒò'.split('').forEach(function(c){NM[c]='o';});
'ūúǔù'.split('').forEach(function(c){NM[c]='u';});
'ǖǘǚǜ'.split('').forEach(function(c){NM[c]='ü';});

function norm(s){
	var r='';for(var i=0;i<s.length;i++)r+=NM[s[i]]||s[i];
	return r.toLowerCase();
}
function tone(s){
	for(var i=0;i<s.length;i++)if(TM[s[i]])return TM[s[i]];
	return 5;
}
function isHz(c){
	var x=c.charCodeAt(0);
	return(x>=0x4E00&&x<=0x9FFF)||(x>=0x3400&&x<=0x4DBF)||(x>=0xF900&&x<=0xFAFF);
}
function countHz(s){var n=0;for(var i=0;i<s.length;i++)if(isHz(s[i]))n++;return n;}
function isPy(c){
	var x=c.charCodeAt(0);
	return(x>=65&&x<=90)||(x>=97&&x<=122)||!!NM[c]||c==='ü';
}

/* Complete valid pinyin syllable table (~410 syllables) */
var SY=new Set(('a,o,e,ai,ei,ao,ou,an,en,ang,eng,er,'+
'ba,bo,bi,bu,bai,bei,bao,ban,ben,bang,beng,bian,biao,bie,bin,bing,'+
'pa,po,pi,pu,pai,pei,pao,pou,pan,pen,pang,peng,pian,piao,pie,pin,ping,'+
'ma,mo,me,mi,mu,mai,mei,mao,mou,man,men,mang,meng,mian,miao,mie,min,ming,miu,'+
'fa,fo,fu,fei,fan,fen,fang,feng,fou,'+
'da,de,di,du,dai,dei,dao,dou,dan,den,dang,deng,dong,dia,dian,diao,die,diu,ding,duan,dui,dun,duo,'+
'ta,te,ti,tu,tai,tei,tao,tou,tan,tang,teng,tong,tian,tiao,tie,ting,tuan,tui,tun,tuo,'+
'na,ne,ni,nu,nü,nai,nei,nao,nou,nan,nen,nang,neng,nong,nia,nian,niang,niao,nie,nin,ning,niu,nuan,nuo,nüe,'+
'la,le,li,lu,lü,lai,lei,lao,lou,lan,lang,leng,long,lia,lian,liang,liao,lie,lin,ling,liu,luan,lun,luo,lüe,'+
'ga,ge,gu,gai,gei,gao,gou,gan,gen,gang,geng,gong,gua,guai,guan,guang,gui,gun,guo,'+
'ka,ke,ku,kai,kei,kao,kou,kan,ken,kang,keng,kong,kua,kuai,kuan,kuang,kui,kun,kuo,'+
'ha,he,hu,hai,hei,hao,hou,han,hen,hang,heng,hong,hua,huai,huan,huang,hui,hun,huo,'+
'ji,ju,jia,jian,jiang,jiao,jie,jin,jing,jiong,jiu,juan,jue,jun,'+
'qi,qu,qia,qian,qiang,qiao,qie,qin,qing,qiong,qiu,quan,que,qun,'+
'xi,xu,xia,xian,xiang,xiao,xie,xin,xing,xiong,xiu,xuan,xue,xun,'+
'zha,zhe,zhi,zhu,zhai,zhei,zhao,zhou,zhan,zhen,zhang,zheng,zhong,zhua,zhuai,zhuan,zhuang,zhui,zhun,zhuo,'+
'cha,che,chi,chu,chai,chao,chou,chan,chen,chang,cheng,chong,chuai,chuan,chuang,chui,chun,chuo,'+
'sha,she,shi,shu,shai,shei,shao,shou,shan,shen,shang,sheng,shua,shuai,shuan,shuang,shui,shun,shuo,'+
'ri,ru,re,rao,rou,ran,ren,rang,reng,rong,ruan,rui,run,ruo,'+
'za,ze,zi,zu,zai,zei,zao,zou,zan,zen,zang,zeng,zong,zuan,zui,zun,zuo,'+
'ca,ce,ci,cu,cai,cao,cou,can,cen,cang,ceng,cong,cuan,cui,cun,cuo,'+
'sa,se,si,su,sai,sao,sou,san,sen,sang,seng,song,suan,sui,sun,suo,'+
'ya,yo,ye,yi,yu,yao,you,yan,yin,yang,ying,yong,yuan,yue,yun,'+
'wa,wo,wu,wai,wei,wan,wen,wang,weng').split(','));

/*
 * Segment normalized pinyin into exactly n syllables via backtracking.
 * Scores by fewest vowel-initial syllables (consonant-initial preferred).
 * Returns best split positions [[start,end],...] or null.
 */
function seg(s,n){
	var best=null;
	(function bt(pos,sp){
		if(sp.length===n){
			if(pos===s.length){
				var vi=0;
				for(var i=0;i<sp.length;i++)
					if('aeiouü'.indexOf(s.charAt(sp[i][0]))>=0)vi++;
				if(!best||vi<best.v)best={s:sp.map(function(x){return x.slice();}),v:vi};
			}
			return;
		}
		var rem=n-sp.length,left=s.length-pos;
		if(left<rem||left>rem*6)return;
		for(var len=Math.min(6,left);len>=1;len--){
			if(SY.has(s.substring(pos,pos+len))){
				sp.push([pos,pos+len]);
				bt(pos+len,sp);
				sp.pop();
				if(best&&best.v===0)return;
			}
		}
	})(0,[]);
	return best?best.s:null;
}

var hzN=chEl?countHz(chEl.textContent):0;

pEls.forEach(function(el){
	if(el.dataset.colorized)return;
	var raw=el.textContent;
	if(!raw.trim()){el.dataset.colorized='true';return;}

	/* Strip non-pinyin chars, build original-position map */
	var stripped='',posMap=[];
	for(var i=0;i<raw.length;i++){
		if(isPy(raw[i])){posMap.push(i);stripped+=raw[i];}
	}

	var normed=norm(stripped);
	var splits=hzN>0?seg(normed,hzN):null;

	if(splits){
		var html='',last=0;
		for(var i=0;i<splits.length;i++){
			var oS=posMap[splits[i][0]];
			var oE=posMap[splits[i][1]-1]+1;
			if(oS>last)html+=raw.substring(last,oS);
			var syl=raw.substring(oS,oE);
			html+='<span class="tone'+tone(syl)+'">'+syl+'</span>';
			last=oE;
		}
		if(last<raw.length)html+=raw.substring(last);
		el.innerHTML=html;
	} else {
		/* Fallback: color each whitespace-separated token by its tone mark */
		el.innerHTML=raw.replace(/\\S+/g,function(w){
			return '<span class="tone'+tone(w)+'">'+w+'</span>';
		});
	}
	el.dataset.colorized='true';
});
})();
</script>
"""

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
				# Recognition: see the character → recall meaning
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
				# Production: see the meaning → recall the character
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
	"""Deterministic Anki GUID derived from a Notion page ID."""
	return genanki.guid_for(notion_id)


def build_deck(rows: list[dict]) -> genanki.Deck:
	model = _make_model()
	deck  = genanki.Deck(DECK_ID, DECK_NAME)

	for row in rows:
		if row.get("exclude"):
			continue
		note = genanki.Note(
			model=model,
			fields=[
				row["chinese"],
				row["pinyin"],
				row["meaning"],
				row["type"],
				row["notes"],
				f"[sound:zh_{row['id']}.mp3]",
			],
			tags=row["tags"],
			guid=_stable_guid(row["id"]),
		)
		deck.add_note(note)
		log.debug("Added note: %s", row["chinese"])

	log.info("Deck built: %d notes", len(deck.notes))
	return deck


# ── Upload backends ────────────────────────────────────────────────────────────

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
		log.info("Media sync complete.")

	try:
		# Login to AnkiWeb
		log.info("Logging in to AnkiWeb as %s…", ANKIWEB_USERNAME)
		auth = col.sync_login(
			username=ANKIWEB_USERNAME,
			password=ANKIWEB_PASSWORD,
			endpoint=None,
		)

		# Sync DOWN first (normal incremental sync)
		log.info("Syncing down from AnkiWeb…")
		sync_result = col.sync_collection(auth=auth, sync_media=True)
		_wait_for_media_sync(col)
		
		# If a full sync was requested by AnkiWeb during pull, download it
		if sync_result.required in (SyncOutput.FULL_SYNC, SyncOutput.FULL_UPLOAD):
			log.info("Full sync required by AnkiWeb during pull — downloading entire collection…")
			if sync_result.new_endpoint:
				auth.endpoint = sync_result.new_endpoint
			col.close_for_full_sync()
			col.full_upload_or_download(
				auth=auth,
				server_usn=None,
				upload=False,
			)
			log.info("Full download complete, reopening collection…")
			col.reopen(after_full_sync=True)

		audio_front = "{{Audio}}" if AUDIO_PLACEMENT in ("front", "both") else ""
		audio_back = "{{Audio}}" if AUDIO_PLACEMENT in ("back", "both") else ""
		audio_back_production = "{{Audio}}" if AUDIO_PLACEMENT in ("front", "back", "both") else ""

		# Get or create note model
		model = col.models.by_name(MODEL_NAME)
		if not model:
			log.info("Creating new model %s in collection…", MODEL_NAME)
			model = col.models.new(MODEL_NAME)
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
			
			if note_ids:
				try:
					card_ids = col.db.list("select id from cards where nid = ?", note_ids[0])
					if card_ids:
						col.sched.unsuspend_cards(card_ids)
						unsuspended_count += len(card_ids)
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
				new_tags = col.tags.canonify(row["tags"])
				if sorted(note.tags) != sorted(new_tags):
					note.tags = new_tags
					changed = True
				
				if changed:
					col.update_note(note)
					updated_count += 1
			else:
				note = col.new_note(model)
				note.guid = guid
				for i, val in enumerate(fields):
					note.fields[i] = val
				note.tags = col.tags.canonify(row["tags"])
				col.add_note(note, deck_id)
				added_count += 1
		
		log.info("Finished notes sync. Added: %d, Updated: %d, Suspended: %d, Unsuspended: %d", added_count, updated_count, suspended_count, unsuspended_count)

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
	finally:
		# close() is safe to call even if close_for_full_sync() already ran
		try:
			col.close()
		except Exception:
			pass

	return None


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


def upload_apkg(file_path: Path, filename: str, rows: list[dict]) -> list[str | None]:
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
		urls.append(upload_ankiweb(file_path, filename, rows))
	return urls


def get_anki_day_cutoff() -> int:
	"""
	Log in to AnkiWeb, sync down to ensure we have the latest collection settings,
	and return the next daily rollover/reset Unix timestamp (col.sched.day_cutoff).
	"""
	import anki.lang
	from anki.collection import Collection
	from anki.sync import SyncOutput

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


def run_once() -> None:
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
		urls = upload_apkg(out_path, filename, rows)
		for url in urls:
			if url:
				log.info("Deck available at: %s", url)
	except Exception as exc:
		log.error("Upload failed: %s", exc, exc_info=True)
		sys.exit(1)

	log.info("=== Sync complete ===")


def run_daemon() -> None:
	log.info("=== Anki Sync Daemon Started ===")

	# Register signal handlers for graceful shutdown
	def handle_shutdown(signum, frame):
		log.info("Received signal %d. Shutting down gracefully…", signum)
		sys.exit(0)

	signal.signal(signal.SIGTERM, handle_shutdown)
	signal.signal(signal.SIGINT, handle_shutdown)

	last_successful_sync_date = None

	while True:
		log.info("Checking AnkiWeb for the latest deck reset time…")
		try:
			day_cutoff = get_anki_day_cutoff()
		except Exception as exc:
			log.error("Failed to check AnkiWeb day cutoff: %s. Retrying in 15 minutes…", exc)
			time.sleep(900)
			continue

		now = int(time.time())
		# Sync 10 minutes before the daily reset window
		target_time = day_cutoff - 600

		# Format target time for logs in local and UTC
		try:
			# Use timezone-aware UTC datetime
			from datetime import timezone
			target_dt_utc = datetime.fromtimestamp(target_time, timezone.utc)
		except ImportError:
			# Fallback if timezone not available in older python
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
	if os.getenv("RUN_AS_DAEMON", "").lower() == "true":
		run_daemon()
	else:
		run_once()


if __name__ == "__main__":
	main()
