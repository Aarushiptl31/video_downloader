
import os
import uuid
import threading
import shutil
import re
import requests

from flask import Flask, request, jsonify, send_from_directory, render_template

import yt_dlp


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "temp_clips")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}


# ---------------------------------------------------------
# TIME HELPERS
# ---------------------------------------------------------

def to_seconds(t):
    parts = [int(p) for p in t.strip().split(":")]

    while len(parts) < 3:
        parts.insert(0, 0)

    h, m, s = parts
    return h * 3600 + m * 60 + s


# ---------------------------------------------------------
# YOUTUBE QUALITY
# ---------------------------------------------------------

QUALITY_MAP = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
    "360": "bv*[height<=360]+ba/b[height<=360]",
}


# ---------------------------------------------------------
# YOUTUBE DATA API
# ---------------------------------------------------------

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")


def extract_video_id(url):
    """
    Extract a YouTube video ID from common YouTube URL formats.
    Supports:
    - youtube.com/watch?v=...
    - youtube.com/live/...
    - youtube.com/shorts/...
    - youtube.com/embed/...
    - youtu.be/...
    """

    from urllib.parse import urlparse, parse_qs

    try:
        parsed = urlparse(url.strip())

        hostname = (parsed.hostname or "").lower()

        # youtube.com URLs
        if hostname in (
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
        ):

            # Standard:
            # https://www.youtube.com/watch?v=VIDEO_ID
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [None])[0]

                if video_id:
                    return video_id[:11]

            # Live:
            # https://www.youtube.com/live/VIDEO_ID
            if parsed.path.startswith("/live/"):
                video_id = parsed.path.split("/live/")[1].split("/")[0]

                if video_id:
                    return video_id[:11]

            # Shorts:
            # https://www.youtube.com/shorts/VIDEO_ID
            if parsed.path.startswith("/shorts/"):
                video_id = parsed.path.split("/shorts/")[1].split("/")[0]

                if video_id:
                    return video_id[:11]

            # Embed:
            # https://www.youtube.com/embed/VIDEO_ID
            if parsed.path.startswith("/embed/"):
                video_id = parsed.path.split("/embed/")[1].split("/")[0]

                if video_id:
                    return video_id[:11]

        # youtu.be:
        # https://youtu.be/VIDEO_ID
        if hostname in (
            "youtu.be",
            "www.youtu.be",
        ):
            video_id = parsed.path.strip("/").split("/")[0]

            if video_id:
                return video_id[:11]

    except Exception:
        pass

    return None

def parse_iso_duration(duration):
    """
    Convert ISO 8601 duration such as PT1H2M30S
    into seconds.
    """

    if not duration:
        return None

    match = re.fullmatch(
        r"PT"
        r"(?:(\d+)H)?"
        r"(?:(\d+)M)?"
        r"(?:(\d+)S)?",
        duration
    )

    if not match:
        return None

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)

    return hours * 3600 + minutes * 60 + seconds


def get_youtube_video_info(video_id):
    """
    Get YouTube metadata through YouTube Data API.
    This does NOT use yt-dlp or YouTube cookies.
    """

    if not YOUTUBE_API_KEY:
        raise RuntimeError(
            "YOUTUBE_API_KEY is not configured on the server."
        )

    api_url = "https://www.googleapis.com/youtube/v3/videos"

    params = {
        "part": "snippet,contentDetails",
        "id": video_id,
        "key": YOUTUBE_API_KEY,
    }

    response = requests.get(
        api_url,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("items"):
        raise RuntimeError(
            "YouTube video not found or unavailable."
        )

    item = data["items"][0]

    snippet = item.get("snippet", {})
    content_details = item.get("contentDetails", {})

    duration = parse_iso_duration(
        content_details.get("duration")
    )

    thumbnails = snippet.get("thumbnails", {})

    thumbnail = None

    if "maxres" in thumbnails:
        thumbnail = thumbnails["maxres"]["url"]
    elif "high" in thumbnails:
        thumbnail = thumbnails["high"]["url"]
    elif "medium" in thumbnails:
        thumbnail = thumbnails["medium"]["url"]
    elif "default" in thumbnails:
        thumbnail = thumbnails["default"]["url"]

    return {
        "title": snippet.get("title"),
        "thumbnail": thumbnail,
        "duration": duration,
        "channel": snippet.get("channelTitle"),
        "video_id": video_id,
    }



# ---------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------

def run_download(job_id, url, start, end, quality):

    jobs[job_id] = {
        "status": "downloading",
        "progress": 0,
        "file": None,
        "error": None
    }

    out_tmpl = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.%(ext)s"
    )

    def hook(d):

        if d["status"] == "downloading":

            p = (
                d.get("_percent_str", "0%")
                .strip()
                .replace("%", "")
            )

            try:
                jobs[job_id]["progress"] = float(p)
            except ValueError:
                pass

        elif d["status"] == "finished":

            jobs[job_id]["progress"] = 100

    try:
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

            "force_keyframes_at_cuts": False,

            "concurrent_fragment_downloads": 8,

            "extractor_args": {
                "youtubepot-bgutilhttp": {
                    "base_url": "http://127.0.0.1:4416"
                }
            },

            "quiet": False,
            "noprogress": True,

            "js_runtimes": {
                "deno": {}
            },

            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,

            "progress_hooks": [hook],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        for filename in os.listdir(DOWNLOAD_DIR):

            if filename.startswith(job_id):

                jobs[job_id]["file"] = filename
                break

        if not jobs[job_id]["file"]:
            raise RuntimeError(
                "Download completed but output file was not found."
            )

        jobs[job_id]["progress"] = 100
        jobs[job_id]["status"] = "done"

    except Exception as e:

        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------
# PREVIEW INFO
# ---------------------------------------------------------

@app.route("/api/preview_info", methods=["POST"])
def preview_info():

    data = request.json or {}

    url = data.get("url", "").strip()

    if not url:
        return jsonify({
            "error": "URL required"
        }), 400

    video_id = extract_video_id(url)

    if not video_id:
        return jsonify({
            "error": "Invalid YouTube URL"
        }), 400

    try:

        # IMPORTANT:
        # This uses YouTube Data API.
        # yt-dlp is NOT called here.
        # YouTube cookies are NOT required here.

        info = get_youtube_video_info(video_id)

        return jsonify(info)

    except requests.HTTPError as e:

        try:
            error_data = e.response.json()

            api_error = (
                error_data
                .get("error", {})
                .get("message")
            )

            if api_error:
                return jsonify({
                    "error": api_error
                }), 400

        except Exception:
            pass

        return jsonify({
            "error": "YouTube API request failed."
        }), 400

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 400


# ---------------------------------------------------------
# START DOWNLOAD
# ---------------------------------------------------------

@app.route("/api/download", methods=["POST"])
def start_download():

    data = request.json or {}

    url = data.get("url", "").strip()

    start = data.get(
        "start",
        "0:00"
    )

    end = data.get(
        "end",
        "0:10"
    )

    quality = data.get(
        "quality",
        "best"
    )

    if not url:
        return jsonify({
            "error": "URL required"
        }), 400

    job_id = uuid.uuid4().hex[:10]

    threading.Thread(
        target=run_download,
        args=(
            job_id,
            url,
            start,
            end,
            quality
        ),
        daemon=True
    ).start()

    return jsonify({
        "job_id": job_id
    })


# ---------------------------------------------------------
# STATUS
# ---------------------------------------------------------

@app.route("/api/status/<job_id>")
def status(job_id):

    return jsonify(
        jobs.get(
            job_id,
            {
                "status": "unknown"
            }
        )
    )


# ---------------------------------------------------------
# PREVIEW CLIP
# ---------------------------------------------------------

@app.route("/api/preview_clip/<job_id>")
def preview_clip(job_id):

    job = jobs.get(job_id)

    if not job or not job.get("file"):

        return jsonify({
            "error": "not ready"
        }), 404

    return send_from_directory(
        DOWNLOAD_DIR,
        job["file"],
        as_attachment=False
    )


# ---------------------------------------------------------
# DOWNLOAD FILE
# ---------------------------------------------------------

@app.route("/api/file/<job_id>")
def get_file(job_id):

    job = jobs.get(job_id)

    if not job or not job.get("file"):

        return jsonify({
            "error": "not ready"
        }), 404

    filename = job["file"]

    response = send_from_directory(
        DOWNLOAD_DIR,
        filename,
        as_attachment=True
    )

    @response.call_on_close
    def cleanup():

        path = os.path.join(
            DOWNLOAD_DIR,
            filename
        )

        if os.path.exists(path):
            os.remove(path)

        jobs.pop(job_id, None)

    return response


# ---------------------------------------------------------
# LOCAL DEVELOPMENT
# ---------------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        ),
        debug=False
    )

