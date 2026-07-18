import io, logging, os, re, shutil, subprocess, threading, time, uuid, zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
import requests
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

DOWNLOAD_ROOT = Path(os.environ.get("GHOSTMP3_DOWNLOAD_DIR", "/tmp/ghostmp3"))
RETENTION_SECONDS = int(os.environ.get("GHOSTMP3_RETENTION_SECONDS", 900))
DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("GHOSTMP3_TIMEOUT_SECONDS", 300))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("GHOSTMP3_MAX_CONCURRENT", 3))
MAX_BATCH_SIZE = 100
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
    id: str; kind: str; query: str; batch_id: Optional[str] = None; status: str = "queued"; message: str = ""; filename: Optional[str] = None; created_at: float = field(default_factory=time.time)

TASKS: Dict[str, Task] = {}
TASKS_LOCK = threading.Lock()

def _task_dir(task_id: str) -> Path: return DOWNLOAD_ROOT / task_id
def _set_task(task_id: str, **updates) -> None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if task:
            for k, v in updates.items(): setattr(task, k, v)
def _task_public(task: Task) -> dict: return {"task_id": task.id, "query": task.query, "status": task.status, "message": task.message, "filename": task.filename}

URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)
def clean_query(raw: str) -> str:
    query = (raw or "").strip()
    if not query: raise ValueError("Empty entry.")
    return query
def resolve_target(query: str) -> str: return query if URL_PATTERN.match(query) else f"ytsearch1:{query}"
def parse_batch_queries(raw_list) -> List[str]:
    if not isinstance(raw_list, list): raise ValueError("Expected a list.")
    cleaned = [clean_query(item) for item in raw_list if (item or "").strip()]
    if not cleaned: raise ValueError("The list is empty.")
    if len(cleaned) > MAX_BATCH_SIZE: raise ValueError(f"Limit is {MAX_BATCH_SIZE}.")
    return cleaned

def _download(task_id: str, kind: str, query: str, quality_key: str) -> None:
    task_dir = _task_dir(task_id); task_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(task_dir / "%(title).150B.%(ext)s"); target = resolve_target(query)
    if kind == "music": cmd = ["yt-dlp", "--no-playlist", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0", "-o", output_template, target]
    else:
        fmt = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS[DEFAULT_QUALITY_KEY]); cmd = ["yt-dlp", "--no-playlist", "-f", fmt, "--merge-output-format", "mp4", "-o", output_template, target]
    _set_task(task_id, status="running", message="Downloading...")
    try: subprocess.run(cmd, check=True, timeout=DOWNLOAD_TIMEOUT_SECONDS, capture_output=True, text=True)
    except Exception as exc:
        err_msg = "Timed out." if isinstance(exc, subprocess.TimeoutExpired) else ((exc.stderr or "").strip().splitlines()[-1:] or ["yt-dlp error."])[0][:160]; _set_task(task_id, status="error", message=err_msg); shutil.rmtree(task_dir, ignore_errors=True); return
    produced = [p for p in task_dir.iterdir() if p.is_file()]
    if not produced: _set_task(task_id, status="error", message="No file produced."); shutil.rmtree(task_dir, ignore_errors=True); return
    result_file = max(produced, key=lambda p: p.stat().st_size); _set_task(task_id, status="done", message="Ready.", filename=result_file.name)

def run_download(task_id: str, kind: str, query: str, quality_key: str) -> None: 
    with download_slots: _download(task_id, kind, query, quality_key)

def _launch(kind: str, query: str, quality_key: str, batch_id: Optional[str] = None) -> Task:
    task_id = uuid.uuid4().hex; task = Task(id=task_id, kind=kind, query=query, batch_id=batch_id)
    with TASKS_LOCK: TASKS[task_id] = task
    threading.Thread(target=run_download, args=(task_id, kind, query, quality_key), name=f"dl-{task_id[:8]}", daemon=True).start(); return task

def reaper_loop() -> None:
    while True:
        cutoff = time.time() - RETENTION_SECONDS
        with TASKS_LOCK: expired = [tid for tid, t in TASKS.items() if t.created_at < cutoff]; [TASKS.pop(tid, None) for tid in expired]
        for tid in expired: shutil.rmtree(_task_dir(tid), ignore_errors=True)
        time.sleep(60)

threading.Thread(target=reaper_loop, name="reaper", daemon=True).start()

@app.route("/")
def index(): return render_template("index.html")
@app.route("/api/download", methods=["POST"])
def api_download():
    payload = request.get_json(silent=True) or {}; kind = payload.get("type")
    if kind not in ("music", "video"): return jsonify({"error": "Invalid type."}), 400
    try: query = clean_query(payload.get("query", ""))
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    quality_key = payload.get("quality", DEFAULT_QUALITY_KEY)
    if quality_key not in QUALITY_PRESETS: quality_key = DEFAULT_QUALITY_KEY
    task = _launch(kind, query, quality_key); return jsonify({"task_id": task.id, "status": task.status})
@app.route("/api/batch", methods=["POST"])
def api_batch():
    payload = request.get_json(silent=True) or {}; kind = payload.get("type")
    if kind not in ("music", "video"): return jsonify({"error": "Invalid type."}), 400
    try: queries = parse_batch_queries(payload.get("queries", []))
    except ValueError as exc: return jsonify({"error": str(exc)}), 400
    quality_key = payload.get("quality", DEFAULT_QUALITY_KEY)
    if quality_key not in QUALITY_PRESETS: quality_key = DEFAULT_QUALITY_KEY
    batch_id = uuid.uuid4().hex; tasks = [_launch(kind, q, quality_key, batch_id=batch_id) for q in queries]
    return jsonify({"batch_id": batch_id, "tasks": [_task_public(t) for t in tasks]})
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
            name = secure_filename(task.filename) or task.filename; final_name = name; n = 1
            while final_name in used_names: stem, dot, ext = name.rpartition("."); final_name = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"; n += 1
            used_names.add(final_name); zf.write(file_path, arcname=final_name)
    buffer.seek(0); return send_file(buffer, as_attachment=True, download_name="GHOSTMP3-Batch.zip", mimetype="application/zip")

if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
