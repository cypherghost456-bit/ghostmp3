import io, logging, os, re, shutil, subprocess, threading, time, uuid, zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
import requests
from flask import Flask, jsonify, render_template_string, request, send_file
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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GHOSTMP3</title>
    <style>
        :root { --bg: #0d0d0d; --surface: #141414; --border: #2a2a2a; --text: #e0e0e0; --text-dim: #888; --accent: #00ffcc; }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background-color: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; align-items: center; padding: 20px; }
        .container { width: 100%; max-width: 600px; }
        .logo-area { text-align: center; margin-bottom: 30px; }
        .logo-icon { font-size: 3rem; margin-bottom: 10px; filter: drop-shadow(0 0 10px rgba(0, 255, 204, 0.5)); }
        h1 { font-size: 2.2rem; font-weight: 800; color: var(--accent); }
        .subtitle { font-size: 0.9rem; color: var(--text-dim); letter-spacing: 2px; text-transform: uppercase; }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 20px; }
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
        .tab { padding: 8px 12px; background: transparent; color: var(--text-dim); border: none; cursor: pointer; font-weight: 600; border-radius: 6px; font-size: 0.9rem; }
        .tab.active { background: rgba(0, 255, 204, 0.1); color: var(--accent); }
        .input-group { margin-bottom: 16px; display: none; }
        .input-group.active { display: block; }
        label { display: block; font-size: 0.85rem; color: var(--text-dim); margin-bottom: 8px; }
        input[type="text"], textarea, select { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 12px; border-radius: 8px; font-size: 1rem; outline: none; }
        input:focus, textarea:focus { border-color: var(--accent); }
        textarea { resize: vertical; min-height: 120px; font-family: monospace; }
        .btn { width: 100%; padding: 14px; background: var(--accent); color: #000; font-weight: 700; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; text-transform: uppercase; letter-spacing: 1px; margin-top: 10px; }
        .btn:hover { opacity: 0.9; }
        .btn:disabled { background: #333; color: #666; cursor: not-allowed; }
        .btn-outline { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
        .status-box { margin-top: 16px; padding: 16px; background: rgba(0,0,0,0.3); border-radius: 8px; display: none; }
        .status-text { color: var(--accent); font-size: 0.9rem; font-weight: 500; word-wrap: break-word; }
        .status-error { color: #ff4757; }
        .progress-bar { width: 100%; height: 4px; background: var(--border); border-radius: 2px; margin-top: 10px; overflow: hidden; display: none; }
        .progress-fill { height: 100%; background: var(--accent); width: 0%; transition: width 0.3s; }
        .watermark { text-align: center; margin-top: 40px; color: #333; font-size: 0.8rem; letter-spacing: 1px; }
        .watermark span { color: #444; font-weight: 600; }
        .hidden { display: none !important; }
        .result-list { margin-top: 15px; }
        .result-item { background: var(--bg); padding: 10px; border-radius: 6px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; border: 1px solid var(--border); }
        .result-item a { color: var(--accent); font-weight: 700; text-decoration: none; }
    </style>
</head>
<body>
<div class="container">
    <div class="logo-area">
        <div class="logo-icon">❄️</div>
        <h1>GHOSTMP3</h1>
        <div class="subtitle">Phantom-Fast Downloader</div>
    </div>
    <div class="card">
        <div class="tabs">
            <button class="tab active" onclick="switchTab('single')">🎵 Single MP3</button>
            <button class="tab" onclick="switchTab('list')">📋 Batch List</button>
            <button class="tab" onclick="switchTab('video')">🎬 Video</button>
        </div>

        <!-- SINGLE MP3 TAB -->
        <div id="tab-single" class="input-group active">
            <label>Song Name or YouTube URL</label>
            <input type="text" id="single-query" placeholder="e.g. Blinding Lights">
            <button class="btn" onclick="startSingle()">Download MP3</button>
            <div class="status-box" id="single-status-box">
                <div class="status-text" id="single-status-text">Initializing...</div>
                <button class="btn btn-outline hidden" id="single-download-btn" style="margin-top:12px;">💾 Save to Phone</button>
            </div>
        </div>

        <!-- BATCH LIST TAB -->
        <div id="tab-list" class="input-group">
            <label>Paste up to 100 tracks (one per line)</label>
            <textarea id="list-queries" placeholder="Nirvana Smells Like Teen Spirit\\nLinkin Park In The End"></textarea>
            <button class="btn" onclick="startList()">Download All as MP3</button>
            <div class="status-box" id="batch-status-box">
                <div class="status-text" id="batch-status-text">Initializing...</div>
                <div class="progress-bar" id="batch-progress-bar"><div class="progress-fill" id="batch-progress-fill"></div></div>
                <div class="result-list" id="batch-results"></div>
                <button class="btn btn-outline hidden" id="batch-zip-btn" style="margin-top:12px;">💾 Download All as ZIP</button>
            </div>
        </div>

        <!-- VIDEO TAB -->
        <div id="tab-video" class="input-group">
            <label>YouTube URL or Video Name</label>
            <input type="text" id="video-query" placeholder="e.g. https://youtu.be/...">
            <select id="video-quality" style="margin-top: 10px;">
                <option value="720p">720p Video (MP4)</option>
                <option value="1080p">1080p Video (MP4)</option>
                <option value="480p">480p Video (MP4)</option>
            </select>
            <button class="btn" onclick="startVideo()">Download Video</button>
            <div class="status-box" id="video-status-box">
                <div class="status-text" id="video-status-text">Initializing...</div>
                <button class="btn btn-outline hidden" id="video-download-btn" style="margin-top:12px;">💾 Save to Phone</button>
            </div>
        </div>
    </div>
    <div class="watermark">GHOSTMP3 &copy; 2024 &mdash; Made by <span>Sumair</span></div>
</div>

<script>
    // TAB SWITCHING
    function switchTab(tab) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.input-group').forEach(t => t.classList.remove('active'));
        event.target.classList.add('active');
        document.getElementById('tab-'+tab).classList.add('active');
    }

    function showStatus(type, msg, isError=false) {
        const box = document.getElementById(type+'-status-box');
        const text = document.getElementById(type+'-status-text');
        box.style.display = 'block';
        text.innerText = msg;
        text.className = isError ? 'status-text status-error' : 'status-text';
    }

    // SINGLE MP3
    async function startSingle() {
        const query = document.getElementById('single-query').value.trim();
        if(!query) return alert("Empty!");
        showStatus('single', "👻 Ghost is hunting...");
        document.getElementById('single-download-btn').classList.add('hidden');
        
        const res = await fetch('/api/download', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({type:'music', query, quality:'720p'}) });
        const data = await res.json();
        if(data.error) return showStatus('single', data.error, true);
        pollStatus('single', data.task_id);
    }

    function pollStatus(type, taskId) {
        fetch('/api/status/'+taskId).then(r=>r.json()).then(data => {
            if(data.status === 'running') {
                showStatus(type, "⏳ Downloading & converting... please wait");
                setTimeout(() => pollStatus(type, taskId), 2000);
            } else if(data.status === 'done') {
                showStatus(type, "✅ Ready! Click below to save.");
                const btn = document.getElementById(type+'-download-btn');
                btn.classList.remove('hidden');
                btn.onclick = () => window.location.href = '/api/file/'+taskId;
            } else if(data.status === 'error') {
                showStatus(type, '❌ ' + data.message, true);
            }
        });
    }

    // VIDEO
    async function startVideo() {
        const query = document.getElementById('video-query').value.trim();
        const quality = document.getElementById('video-quality').value;
        if(!query) return alert("Empty!");
        showStatus('video', "👻 Ghost is hunting...");
        document.getElementById('video-download-btn').classList.add('hidden');

        const res = await fetch('/api/download', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({type:'video', query, quality}) });
        const data = await res.json();
        if(data.error) return showStatus('video', data.error, true);
        pollStatus('video', data.task_id);
    }

    // BATCH LIST
    async function startList() {
        const raw = document.getElementById('list-queries').value.split('\\n').filter(l => l.trim());
        if(!raw.length) return alert("Empty!");
        showStatus('batch', "⏳ Starting batch...");
        document.getElementById('batch-progress-bar').style.display = 'block';
        document.getElementById('batch-results').innerHTML = '';
        document.getElementById('batch-zip-btn').classList.add('hidden');

        const res = await fetch('/api/batch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({type:'music', queries:raw, quality:'720p'}) });
        const data = await res.json();
        if(data.error) return showStatus('batch', data.error, true);
        pollBatch(data.batch_id, data.tasks.length);
    }

    function pollBatch(batchId, total) {
        fetch('/api/batch/'+batchId+'/status').then(r=>r.json()).then(data => {
            const tasks = data.tasks;
            const done = tasks.filter(t => t.status === 'done').length;
            const errors = tasks.filter(t => t.status === 'error').length;
            const finished = done + errors;
            const pct = (finished / total) * 100;

            document.getElementById('batch-progress-fill').style.width = pct+'%';
            showStatus('batch', "⏳ Progress: "+done+"/"+total+" done" + (errors > 0 ? " ("+errors+" failed)" : ""));

            // Update individual buttons
            let html = '';
            tasks.forEach(t => {
                if(t.status === 'done') {
                    html += '<div class="result-item"><span>✅ '+t.query+'</span><a href="/api/file/'+t.task_id+'">💾 Save</a></div>';
                } else if(t.status === 'error') {
                    html += '<div class="result-item"><span style="color:#ff4757">❌ '+t.query+'</span></div>';
                }
            });
            document.getElementById('batch-results').innerHTML = html;

            if(finished === total) {
                showStatus('batch', "✅ Batch Complete!");
                document.getElementById('batch-zip-btn').classList.remove('hidden');
                document.getElementById('batch-zip-btn').onclick = () => window.location.href = '/api/batch/'+batchId+'/zip';
            } else {
                setTimeout(() => pollBatch(batchId, total), 3000);
            }
        });
    }
</script>
</body>
</html>
"""

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
def index(): return render_template_string(HTML_TEMPLATE)
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
