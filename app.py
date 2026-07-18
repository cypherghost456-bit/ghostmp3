"""
GHOSTMP3 — A professional self-hosted front end for yt-dlp.
"""

import io, logging, os, re, shutil, subprocess, threading, time, uuid, zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
import requests
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

# Configuration
DOWNLOAD_ROOT = Path(os.environ.get("GHOSTMP3_DOWNLOAD_DIR", "/tmp/ghostmp3"))
RETENTION_SECONDS = int(os.environ.get("GHOSTMP3_RETENTION_SECONDS", 15 * 60))
DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("GHOSTMP3_TIMEOUT_SECONDS", 300))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("GHOSTMP3_MAX_CONCURRENT", 3))
MAX_QUERY_LENGTH = 200
MAX_BATCH_SIZE = 100

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

QUALITY_PRESETS = {
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "best": "bestvideo+bestaudio/best",
}
DEFAULT_QUALITY_KEY = "720p"

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
log = logging.getLogger("ghostmp3")

app = Flask(__name__)
download_slots = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)

@dataclass
class Task:
    id: str
    kind: str
    query: str
    batch_id: Optional[str] = None
    status: str = "queued"
    message: str = ""
    filename: Optional[str] = None
    created_at: float = field(default_factory=time.time)

TASKS: Dict[str, Task] = {}
TASKS_LOCK = threading.Lock()

def _task_dir(task_id: str) -> Path: return DOWNLOAD_ROOT / task_id
def _set_task(task_id: str, **updates) -> None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if task:
            for k, v in updates.items(): setattr(task, k, v)

def _task_public(task: Task) -> dict:
    return {"task_id": task.id, "query": task.query, "status": task.status, "message": task.message, "filename": task.filename}

URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)

def clean_query(raw: str) -> str:
    query = (raw or "").strip()
    if not query: raise ValueError("Empty entry.")
    if len(query) > MAX_QUERY_LENGTH: raise ValueError(f"Keep each entry under {MAX_QUERY_LENGTH} characters.")
    return query

def resolve_target(query: str) -> str:
    return query if URL_PATTERN.match(query) else f"ytsearch1:{query}"

def parse_batch_queries(raw_list) -> List[str]:
    if not isinstance(raw_list, list): raise ValueError("Expected a list of entries.")
    cleaned = [clean_query(item) for item in raw_list if (item or "").strip()]
    if not cleaned: raise ValueError("The list is empty.")
    if len(cleaned) > MAX_BATCH_SIZE: raise ValueError(f"Limit is {MAX_BATCH_SIZE} entries per list.")
    return cleaned

class SpotifyError(Exception): pass

_spotify_token_lock = threading.Lock()
_spotify_token = None
_spotify_token_expires_at = 0.0
SPOTIFY_PLAYLIST_URL_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-\w+/)?playlist/|spotify:playlist:)([a-zA-Z0-9]+)")

def extract_playlist_id(url: str) -> str:
    match = SPOTIFY_PLAYLIST_URL_RE.search((url or "").strip())
    if not match: raise SpotifyError("That doesn't look like a Spotify playlist link.")
    return match.group(1)

def get_spotify_token() -> str:
    global _spotify_token, _spotify_token_expires_at
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise SpotifyError("Spotify import isn't configured on this server (Missing API Keys).")
    
    with _spotify_token_lock:
        if _spotify_token and time.time() < _spotify_token_expires_at: return _spotify_token
        try:
            resp = requests.post("https://accounts.spotify.com/api/token", 
                                 data={"grant_type": "client_credentials"}, 
                                 auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET), timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SpotifyError(f"Couldn't reach Spotify: {exc}") from exc

        payload = resp.json()
        _spotify_token = payload["access_token"]
        _spotify_token_expires_at = time.time() + payload.get("expires_in", 3600) - 60
        return _spotify_token

def fetch_spotify_playlist(playlist_id: str, max_tracks: int = MAX_BATCH_SIZE) -> dict:
    token = get_spotify_token()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        meta_resp = requests.get(f"https://api.spotify.com/v1/playlists/{playlist_id}", 
                                 headers=headers, params={"fields": "name,owner(display_name),images,tracks.total"}, timeout=10)
        meta_resp.raise_for_status()
    except requests.RequestException as exc:
        raise SpotifyError(f"Couldn't reach Spotify: {exc}") from exc

    meta = meta_resp.json()
    playlist_name = meta.get("name") or "Untitled playlist"
    images = meta.get("images") or []
    cover = images[0]["url"] if images else None
    total_available = (meta.get("tracks") or {}).get("total", 0)

    tracks = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params = {"fields": "items(track(name,artists(name),is_local)),next", "limit": 100}

    while url and len(tracks) < max_tracks:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SpotifyError(f"Couldn't read tracks: {exc}") from exc
            
        data = resp.json()
        for item in data.get("items", []):
            track = item.get("track")
            if not track or track.get("is_local"): continue
            name = (track.get("name") or "").strip()
            artists = ", ".join(a["name"] for a in track.get("artists", []) if a.get("name"))
            if not name: continue
            tracks.append({"name": name, "artists": artists, "query": f"{name} {artists}".strip()})
        url = data.get("next")
        params = None

    return {"name": playlist_name, "owner": (meta.get("owner") or {}).get("display_name", ""), "cover": cover, "total_available": total_available, "tracks": tracks}

def _download(task_id: str, kind: str, query: str, quality_key: str) -> None:
    task_dir = _task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(task_dir / "%(title).150B.%(ext)s")
    target = resolve_target(query)

    if kind == "music":
        cmd = ["yt-dlp", "--no-playlist", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0", "-o", output_template, target]
    else:
        fmt = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS[DEFAULT_QUALITY_KEY])
        cmd = ["yt-dlp", "--no-playlist", "-f", fmt, "--merge-output-format", "mp4", "-o", output_template, target]

    _set_task(task_id, status="running", message="Downloading…")
    try:
        subprocess.run(cmd, check=True, timeout=DOWNLOAD_TIMEOUT_SECONDS, capture_output=True, text=True)
    except Exception as exc:
        err_msg = "Timed out." if isinstance(exc, subprocess.TimeoutExpired) else ((exc.stderr or "").strip().splitlines()[-1:] or ["yt-dlp error."])[0][:160]
        _set_task(task_id, status="error", message=err_msg)
        shutil.rmtree(task_dir, ignore_errors=True)
        return

    produced = [p for p in task_dir.iterdir() if p.is_file()]
    if not produced:
        _set_task(task_id, status="error", message="No file produced.")
        shutil.rmtree(task_dir, ignore_errors=True)
        return

    result_file = max(produced, key=lambda p: p.stat().st_size)
    _set_task(task_id, status="done", message="Ready.", filename=result_file.name)

def run_download(task_id: str, kind: str, query: str, quality_key: str) -> None:
    with download_slots: _download(task_id, kind, query, quality_key)

def _launch(kind: str, query: str, quality_key: str, batch_id: Optional[str] = None) -> Task:
    task_id = uuid.uuid4().hex
    task = Task(id=task_id, kind=kind, query=query, batch_id=batch_id)
    with TASKS_LOCK: TASKS[task_id] = task
    threading.Thread(target=run_download, args=(task_id, kind, query, quality_key), name=f"dl-{task_id[:8]}", daemon=True).start()
    return task

def reaper_loop() -> None:
    while True:
        cutoff = time.time() - RETENTION_SECONDS
        with TASKS_LOCK:
            expired = [tid for tid, t in TASKS.items() if t.created_at < cutoff]
            for tid in expired: TASKS.pop(tid, None)
        for tid in expired: shutil.rmtree(_task_dir(tid), ignore_errors=True)
        time.sleep(60)

threading.Thread(target=reaper_loop, name="reaper", daemon=True).start()

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/download", methods=["POST"])
def api_download():
    payload = request.get_json(silent=True) or {}
    kind = payload.get("type")
    if kind not in ("music", "video"): return jsonify({"error": "Invalid type."}), 400
    try: query = clean_query(payload.get("query", ""))
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    quality_key = payload.get("quality", DEFAULT_QUALITY_KEY)
    if quality_key not in QUALITY_PRESETS: quality_key = DEFAULT_QUALITY_KEY
    task = _launch(kind, query, quality_key)
    return jsonify({"task_id": task.id, "status": task.status})

@app.route("/api/batch", methods=["POST"])
def api_batch():
    payload = request.get_json(silent=True) or {}
    kind = payload.get("type")
    if kind not in ("music", "video"): return jsonify({"error": "Invalid type."}), 400
    try: queries = parse_batch_queries(payload.get("queries", []))
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    quality_key = payload.get("quality", DEFAULT_QUALITY_KEY)
    if quality_key not in QUALITY_PRESETS: quality_key = DEFAULT_QUALITY_KEY
    batch_id = uuid.uuid4().hex
    tasks = [_launch(kind, q, quality_key, batch_id=batch_id) for q in queries]
    return jsonify({"batch_id": batch_id, "tasks": [_task_public(t) for t in tasks]})

@app.route("/api/spotify/resolve", methods=["POST"])
def api_spotify_resolve():
    payload = request.get_json(silent=True) or {}
    try:
        playlist_id = extract_playlist_id(payload.get("url", ""))
        playlist = fetch_spotify_playlist(playlist_id, max_tracks=MAX_BATCH_SIZE)
    except SpotifyError as exc: return jsonify({"error": str(exc)}), 400
    if not playlist["tracks"]: return jsonify({"error": "No readable tracks."}), 400
    return jsonify(playlist)

@app.route("/api/status/<task_id>")
def api_status(task_id):
    task = TASKS.get(task_id)
    if not task: return jsonify({"error": "Unknown task."}), 404
    return jsonify(_task_public(task))

@app.route("/api/batch/<batch_id>/status")
def api_batch_status(batch_id):
    with TASKS_LOCK: tasks = [t for t in TASKS.values() if t.batch_id == batch_id]
    if not tasks: return jsonify({"error": "Unknown batch."}), 404
    return jsonify({"tasks": [_task_public(t) for t in tasks]})

@app.route("/api/file/<task_id>")
def api_file(task_id):
    task = TASKS.get(task_id)
    if not task or task.status != "done" or not task.filename: return jsonify({"error": "File not ready."}), 404
    file_path = _task_dir(task_id) / task.filename
    if not file_path.is_file(): return jsonify({"error": "File expired."}), 404
    return send_file(file_path, as_attachment=True, download_name=secure_filename(task.filename))

@app.route("/api/batch/<batch_id>/zip")
def api_batch_zip(batch_id):
    with TASKS_LOCK: tasks = [t for t in TASKS.values() if t.batch_id == batch_id and t.status == "done"]
    if not tasks: return jsonify({"error": "Nothing finished."}), 404
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for task in tasks:
            file_path = _task_dir(task.id) / task.filename
            if not file_path.is_file(): continue
            name = secure_filename(task.filename) or task.filename
            final_name, n = name, 1
            while final_name in used_names:
                stem, dot, ext = name.rpartition(".")
                final_name = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
                n += 1
            used_names.add(final_name)
            zf.write(file_path, arcname=final_name)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="GHOSTMP3-Batch.zip", mimetype="application/zip")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))