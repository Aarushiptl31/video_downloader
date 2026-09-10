import os, uuid, threading
from flask import Flask, request, jsonify, send_from_directory, render_template

import yt_dlp

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "temp_clips")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

def to_seconds(t):
    parts = [int(p) for p in t.strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s

QUALITY_MAP = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
    "360": "bv*[height<=360]+ba/b[height<=360]",
}

def run_download(job_id, url, start, end, quality):
    jobs[job_id] = {"status": "downloading", "progress": 0, "file": None, "error": None}
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    def hook(d):
        if d["status"] == "downloading":
            p = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                jobs[job_id]["progress"] = float(p)
            except ValueError:
                pass
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 100

    ydl_opts = {
    "format": QUALITY_MAP.get(
        quality,
        QUALITY_MAP["best"]
    ),

    "merge_output_format": "mp4",
    "outtmpl": out_tmpl,

    "download_ranges": yt_dlp.utils.download_range_func(
        None,
        [(to_seconds(start), to_seconds(end))]
    ),

    "force_keyframes_at_cuts": True,

    "postprocessors": [
        {
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4"
        }
    ],

    "extractor_args": {
        "youtubepot-bgutilhttp": {
            "base_url": "http://127.0.0.1:4416"
        }
    },

    "progress_hooks": [hook],
    "quiet": True,
    "noprogress": True,
}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(job_id):
                jobs[job_id]["file"] = f
                break
        jobs[job_id]["status"] = "done"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/preview_info", methods=["POST"])
def preview_info():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify({
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    start = data.get("start", "0:00")
    end = data.get("end", "0:10")
    quality = data.get("quality", "best")
    if not url:
        return jsonify({"error": "URL required"}), 400
    job_id = uuid.uuid4().hex[:10]
    threading.Thread(target=run_download, args=(job_id, url, start, end, quality), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def status(job_id):
    return jsonify(jobs.get(job_id, {"status": "unknown"}))

@app.route("/api/preview_clip/<job_id>")
def preview_clip(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("file"):
        return jsonify({"error": "not ready"}), 404
    return send_from_directory(DOWNLOAD_DIR, job["file"], as_attachment=False)

@app.route("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("file"):
        return jsonify({"error": "not ready"}), 404
    filename = job["file"]
    response = send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

    @response.call_on_close
    def cleanup():
        path = os.path.join(DOWNLOAD_DIR, filename)
        if os.path.exists(path):
            os.remove(path)
        jobs.pop(job_id, None)

    return response

if __name__ == "__main__":
    import os
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
