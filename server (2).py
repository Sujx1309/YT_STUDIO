#!/usr/bin/env python3
"""
YT.STUDIO Server — Render + Termux compatible
- Render: /api/stream — direct browser download (no disk needed)
- Termux: /api/download + /api/file — save to disk then serve
"""

import http.server
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", 8765))
IS_RENDER = bool(os.environ.get("RENDER"))

if IS_RENDER:
    DOWNLOAD_DIR = "/tmp/downloads"
else:
    DOWNLOAD_DIR = os.path.expanduser("~/storage/downloads")
    if not os.path.exists(DOWNLOAD_DIR):
        DOWNLOAD_DIR = os.path.expanduser("~/downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

progress_store = {}  # job_id -> dict


# ── URL CLEANER ──────────────────────────────────────────────
def clean_youtube_url(url):
    """youtu.be, ?si=, m.youtube.com, /shorts/, /live/ — badha clean karo"""
    try:
        p = urllib.parse.urlparse(url.strip())
        host = p.netloc.lower().replace("www.", "").replace("m.", "")
        if host == "youtu.be":
            vid = p.path.strip("/").split("/")[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
        if host in ("youtube.com", "music.youtube.com"):
            qs = urllib.parse.parse_qs(p.query)
            vid = qs.get("v", [None])[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
            if "/shorts/" in p.path:
                vid = p.path.split("/shorts/")[1].split("/")[0].split("?")[0]
                return f"https://www.youtube.com/watch?v={vid}"
            if "/live/" in p.path:
                vid = p.path.split("/live/")[1].split("/")[0].split("?")[0]
                return f"https://www.youtube.com/watch?v={vid}"
    except Exception:
        pass
    return url


# ── SYSTEM CHECKS ────────────────────────────────────────────
def check_ytdlp():
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        return r.returncode == 0, r.stdout.strip()
    except FileNotFoundError:
        return False, None

def check_ffmpeg():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


# ── VIDEO INFO ───────────────────────────────────────────────
def get_video_info(url):
    url = clean_youtube_url(url)
    try:
        r = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            d = json.loads(r.stdout)
            return {
                "title": d.get("title", "Unknown"),
                "thumbnail": d.get("thumbnail", ""),
                "duration": d.get("duration_string", ""),
                "uploader": d.get("uploader", ""),
                "view_count": d.get("view_count", 0),
                "age_limit": d.get("age_limit", 0),
                "is_live": d.get("is_live", False),
                "channel_follower_count": d.get("channel_follower_count"),
                "formats": [
                    {"format_id": f.get("format_id"), "ext": f.get("ext"),
                     "height": f.get("height"), "filesize": f.get("filesize"),
                     "vcodec": f.get("vcodec"), "acodec": f.get("acodec")}
                    for f in d.get("formats", [])
                    if f.get("height") or f.get("acodec") != "none"
                ][-20:]
            }
        else:
            return {"error": r.stderr[:400]}
    except Exception as e:
        return {"error": str(e)}


# ── BUILD YT-DLP CMD (shared) ────────────────────────────────
def build_ytdlp_cmd(url, opts, output_template, has_ffmpeg):
    mode        = opts.get("mode", "mp4")
    quality     = opts.get("quality", "1080")
    audio_q     = str(opts.get("audio_quality", "2"))
    playlist    = opts.get("playlist", False)
    subtitles   = opts.get("subtitles", False)
    sub_langs   = opts.get("sub_langs", "en")
    embed_thumb = opts.get("embed_thumbnail", False)
    add_meta    = opts.get("add_metadata", False)
    sponsorblk  = opts.get("sponsorblock", False)
    sb_action   = opts.get("sponsorblock_action", "remove")
    chapters    = opts.get("split_chapters", False)
    no_overwr   = opts.get("no_overwrite", False)
    info_json   = opts.get("write_info_json", False)
    clip_start  = opts.get("clip_start")
    clip_end    = opts.get("clip_end")
    rate_limit  = opts.get("rate_limit")
    sleep_min   = opts.get("sleep_min")
    sleep_max   = opts.get("sleep_max")
    proxy       = opts.get("proxy")
    cookies     = opts.get("cookies_file")
    geo         = opts.get("geo_bypass")
    age_limit   = opts.get("age_limit")
    pl_start    = opts.get("playlist_start")
    pl_end      = opts.get("playlist_end")
    pl_items    = opts.get("playlist_items")
    extra_args  = opts.get("extra_args", "")
    conc_frags  = opts.get("concurrent_frags", 1)
    retries     = opts.get("retries", "10")

    cmd = ["yt-dlp"]

    # FORMAT
    if mode == "mp3":
        if has_ffmpeg:
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", audio_q]
        else:
            cmd += ["-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"]
    elif mode == "gif":
        cmd += ["-f", "worst[ext=mp4]/worst"]
    else:
        if has_ffmpeg:
            if quality == "max":
                fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
            else:
                fmt = (f"bestvideo[ext=mp4][height<={quality}]+bestaudio[ext=m4a]"
                       f"/bestvideo[height<={quality}]+bestaudio"
                       f"/best[height<={quality}]/best")
            cmd += ["-f", fmt, "--merge-output-format", "mp4"]
        else:
            if quality == "max":
                fmt = "best[ext=mp4]/best"
            else:
                fmt = f"best[ext=mp4][height<={quality}]/best[ext=mp4]/best[height<={quality}]/best"
            cmd += ["-f", fmt]

    cmd += ["-o", output_template]

    if not playlist:
        cmd += ["--no-playlist"]
    if pl_start:  cmd += ["--playlist-start", str(pl_start)]
    if pl_end:    cmd += ["--playlist-end",   str(pl_end)]
    if pl_items:  cmd += ["--playlist-items",  pl_items]
    if subtitles and mode != "mp3":
        cmd += ["--write-auto-sub", "--embed-subs", "--sub-langs", sub_langs or "en"]
    if embed_thumb and has_ffmpeg:
        cmd += ["--embed-thumbnail"]
    if add_meta and has_ffmpeg:
        cmd += ["--embed-metadata"]
    if sponsorblk:
        if sb_action == "remove":
            cmd += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]
        elif sb_action == "mark":
            cmd += ["--sponsorblock-mark", "sponsor,selfpromo,interaction"]
        elif sb_action == "chapter":
            cmd += ["--sponsorblock-chapter", "sponsor,selfpromo,interaction"]
    if chapters:     cmd += ["--split-chapters"]
    if no_overwr:    cmd += ["--no-overwrites"]
    if info_json:    cmd += ["--write-info-json"]
    if rate_limit:   cmd += ["--rate-limit", rate_limit]
    if conc_frags:   cmd += ["--concurrent-fragments", str(conc_frags)]
    if retries:      cmd += ["--retries", str(retries)]
    if proxy:        cmd += ["--proxy", proxy]
    if cookies:      cmd += ["--cookies", cookies]
    if geo:          cmd += ["--geo-bypass-country", geo]
    if age_limit:    cmd += ["--age-limit", str(age_limit)]
    if sleep_min and sleep_max:
        cmd += ["--sleep-interval", str(sleep_min), "--max-sleep-interval", str(sleep_max)]
    if clip_start or clip_end:
        section = "*" + (clip_start or "") + "-" + (clip_end or "")
        cmd += ["--download-sections", section]
    if extra_args:
        cmd += shlex.split(extra_args)

    cmd += ["--no-warnings", url]
    return cmd


# ── DISK DOWNLOAD (Termux) ────────────────────────────────────
def download_video(job_id, url, opts):
    url = clean_youtube_url(url)
    progress_store[job_id] = {"status": "downloading", "percent": 0,
                               "speed": "", "eta": "", "filename": "", "filepath": ""}
    out_tmpl = opts.get("output_template") or "%(title)s.%(ext)s"
    output_template = os.path.join(DOWNLOAD_DIR, out_tmpl)
    has_ffmpeg = check_ffmpeg()
    cmd = build_ytdlp_cmd(url, opts, output_template, has_ffmpeg)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, bufsize=1)
        stderr_lines = []
        already_done = False

        def read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line.strip())

        threading.Thread(target=read_stderr, daemon=True).start()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            m = re.search(r'\[download\]\s+([\d.]+)%.*?at\s+([\S]+)\s+ETA\s+([\S]+)', line)
            if m:
                pct = float(m.group(1))
                progress_store[job_id].update({"percent": pct, "speed": m.group(2), "eta": m.group(3)})
                if pct >= 100:
                    already_done = True
            m2 = re.search(r'Destination:\s+(.+)', line)
            if m2:
                fp = m2.group(1).strip()
                progress_store[job_id]["filename"] = os.path.basename(fp)
                progress_store[job_id]["filepath"] = fp
            if "has already been downloaded" in line:
                already_done = True
                fn = re.search(r'\[download\] (.+) has already', line)
                if fn:
                    progress_store[job_id]["filename"] = os.path.basename(fn.group(1))
            if any(x in line for x in ["Merging formats", "Merger", "[ffmpeg]", "Deleting original"]):
                progress_store[job_id]["status"] = "merging"
                progress_store[job_id]["percent"] = 99
            if "Destination:" in line and any(line.endswith(e) for e in [".mp3",".mp4",".m4a",".webm",".mkv"]):
                already_done = True

        proc.wait()
        if proc.returncode == 0 or already_done:
            progress_store[job_id]["status"] = "done"
            progress_store[job_id]["percent"] = 100
        else:
            err = " ".join(stderr_lines[-3:]) if stderr_lines else "yt-dlp error"
            if "WARNING" in err and "ERROR" not in err:
                progress_store[job_id]["status"] = "done"
                progress_store[job_id]["percent"] = 100
            else:
                progress_store[job_id]["status"] = "error"
                progress_store[job_id]["error"] = err[:300]
    except Exception as e:
        progress_store[job_id]["status"] = "error"
        progress_store[job_id]["error"] = str(e)


# ── STREAM DOWNLOAD (Render) ──────────────────────────────────
def stream_video_to_response(handler, url, opts):
    """
    yt-dlp output ne pipe through karine directly browser ne moklo.
    /tmp disk par minimal use. Title fetch kari ne filename set karo.
    """
    url = clean_youtube_url(url)
    has_ffmpeg = check_ffmpeg()
    mode = opts.get("mode", "mp4")

    # Filename mate title fetch karo
    title = "video"
    ext = "mp4" if mode != "mp3" else "mp3"
    try:
        info_r = subprocess.run(
            ["yt-dlp", "--print", "%(title)s|||%(ext)s", "--no-playlist", url],
            capture_output=True, text=True, timeout=20
        )
        if info_r.returncode == 0:
            parts = info_r.stdout.strip().split("|||")
            title = re.sub(r'[\\/*?:"<>|]', '_', parts[0].strip()) if parts[0] else "video"
            if len(parts) > 1 and parts[1]:
                ext = parts[1].strip()
    except Exception:
        pass

    # Streaming mate pipe output use karo
    tmp_file = os.path.join("/tmp", f"ytstream_{uuid.uuid4().hex}.{ext}")
    output_template = tmp_file

    cmd = build_ytdlp_cmd(url, opts, output_template, has_ffmpeg)

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        handler.send_error(504, "Timeout — video download timed out")
        return
    except Exception as e:
        handler.send_error(500, str(e))
        return

    # Actual file find karo (ffmpeg merge thay to ext badlay)
    actual_file = tmp_file
    if not os.path.exists(actual_file):
        base = tmp_file.rsplit(".", 1)[0]
        for candidate_ext in ["mp4", "mkv", "webm", "mp3", "m4a", "opus"]:
            candidate = f"{base}.{candidate_ext}"
            if os.path.exists(candidate):
                actual_file = candidate
                ext = candidate_ext
                break

    if not os.path.exists(actual_file):
        stderr_text = proc.stderr.decode("utf-8", errors="replace")[-400:] if proc.stderr else "Unknown error"
        handler.send_json({"error": stderr_text}, 500)
        return

    mime_map = {
        "mp4": "video/mp4", "mp3": "audio/mpeg", "m4a": "audio/mp4",
        "webm": "video/webm", "mkv": "video/x-matroska",
        "opus": "audio/opus", "ogg": "audio/ogg"
    }
    mime = mime_map.get(ext, "application/octet-stream")
    safe_title = title[:80]
    fname = f"{safe_title}.{ext}"
    file_size = os.path.getsize(actual_file)

    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", file_size)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Disposition", f'attachment; filename="{fname}"')
    handler.end_headers()

    try:
        with open(actual_file, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            os.remove(actual_file)
        except Exception:
            pass


# ── HTTP HANDLER ──────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def send_error_json(self, msg, code=500):
        self.send_json({"error": msg}, code)

    def send_static_file(self, filename, content_type):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.exists(path):
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(body))
            if filename == "sw.js":
                self.send_header("Service-Worker-Allowed", "/")
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(f"{filename} not found".encode())

    def send_html_file(self):
        html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "YT_STUDIO.html")
        if os.path.exists(html):
            with open(html, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"YT_STUDIO.html not found! Same folder ma rakho.")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_html_file()

        elif path == "/sw.js":
            self.send_static_file("sw.js", "application/javascript")
        elif path == "/manifest.json":
            self.send_static_file("manifest.json", "application/manifest+json")
        elif path in ("/icon-128.png", "/icon-192.png", "/icon-512.png"):
            self.send_static_file(path[1:], "image/png")

        elif path == "/api/status":
            ok, ver = check_ytdlp()
            self.send_json({
                "installed": ok, "version": ver,
                "ffmpeg": check_ffmpeg(),
                "download_dir": DOWNLOAD_DIR,
                "is_render": IS_RENDER
            })

        elif path == "/api/info":
            url = params.get("url", [""])[0]
            if not url:
                self.send_json({"error": "URL required"}, 400)
                return
            self.send_json(get_video_info(url))

        elif path == "/api/progress":
            job_id = params.get("id", [""])[0]
            if job_id in progress_store:
                self.send_json(progress_store[job_id])
            else:
                self.send_json({"status": "not_found"}, 404)

        elif path == "/api/file":
            # Termux: disk par saved file serve karo
            job_id = params.get("id", [""])[0]
            job = progress_store.get(job_id)
            if not job or job.get("status") != "done":
                self.send_response(404); self.end_headers()
                self.wfile.write(b"File taiyar nathi"); return
            filepath = job.get("filepath") or os.path.join(DOWNLOAD_DIR, job.get("filename",""))
            if not filepath or not os.path.exists(filepath):
                self.send_response(404); self.end_headers()
                self.wfile.write(b"File disk par malyi nahi"); return
            ext = os.path.splitext(filepath)[1].lower().lstrip(".")
            mime_map = {"mp4":"video/mp4","mp3":"audio/mpeg","m4a":"audio/mp4",
                        "webm":"video/webm","mkv":"video/x-matroska","opus":"audio/opus"}
            mime = mime_map.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", os.path.getsize(filepath))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(filepath)}"')
            self.end_headers()
            try:
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path

        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({"error": "Invalid request"}, 400)
            return

        urls = body.get("urls") or []
        if not urls:
            single = body.get("url", "")
            if single:
                urls = [single]

        if path == "/api/stream":
            # ── RENDER: direct stream to browser ──
            if not urls:
                self.send_json({"error": "URL nai malyo"}, 400)
                return
            url = clean_youtube_url(urls[0])
            # This blocks until download done, then streams
            stream_video_to_response(self, url, body)

        elif path == "/api/download":
            # ── TERMUX: background job, poll progress ──
            if not urls:
                self.send_json({"error": "URL nai malyo"}, 400)
                return
            job_id = str(int(time.time() * 1000))
            t = threading.Thread(target=download_video,
                                 args=(job_id, urls[0], body), daemon=True)
            t.start()
            self.send_json({"job_id": job_id})

        else:
            self.send_response(404); self.end_headers()


# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    ok, ver = check_ytdlp()
    print("=" * 50)
    print("  YT.STUDIO Server")
    print("=" * 50)
    if not ok:
        print("\n⚠️  yt-dlp install nathi!")
        print("  pip install yt-dlp")
    else:
        print(f"\n✓  yt-dlp v{ver} ready")
    print(f"✓  Downloads: {DOWNLOAD_DIR}")
    if IS_RENDER:
        print(f"\n  Render deploy — port {PORT}")
        print("  Streaming mode active (no disk save)")
    else:
        print(f"\n  http://localhost:{PORT}")
        print("  Band karva: Ctrl+C")
    print("=" * 50)

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer band thayo.")
