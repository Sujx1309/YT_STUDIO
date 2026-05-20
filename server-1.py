#!/usr/bin/env python3
"""
YDROP Termux Server
Run: python server.py
Then open: http://localhost:8765
"""

import http.server
import json
import os
import re
import shlex
import subprocess
import threading
import time
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# Render sets PORT env var; fallback to 8765 for local/Termux
PORT = int(os.environ.get("PORT", 8765))

# Download directory — Render uses /tmp (ephemeral), Termux uses ~/storage/downloads
if os.environ.get("RENDER"):
    # On Render: use /tmp/downloads (ephemeral, resets on restart)
    DOWNLOAD_DIR = "/tmp/downloads"
else:
    # Local / Termux
    DOWNLOAD_DIR = os.path.expanduser("~/storage/downloads")
    if not os.path.exists(DOWNLOAD_DIR):
        DOWNLOAD_DIR = os.path.expanduser("~/downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Store active download progress
progress_store = {}

def check_ytdlp():
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        return result.returncode == 0, result.stdout.strip()
    except FileNotFoundError:
        return False, None

def check_ffmpeg():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False

def get_video_info(url):
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist",
             "--extractor-args", "youtube:player_client=web,default",
             url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                "title": data.get("title", "Unknown"),
                "thumbnail": data.get("thumbnail", ""),
                "duration": data.get("duration_string", ""),
                "uploader": data.get("uploader", ""),
                "view_count": data.get("view_count", 0),
                "formats": [
                    {"format_id": f.get("format_id"), "ext": f.get("ext"), "height": f.get("height"),
                     "filesize": f.get("filesize"), "vcodec": f.get("vcodec"), "acodec": f.get("acodec")}
                    for f in data.get("formats", [])
                    if f.get("height") or f.get("acodec") != "none"
                ][-20:]
            }
        else:
            return {"error": result.stderr[:300]}
    except Exception as e:
        return {"error": str(e)}

def download_video(job_id, url, opts):
    progress_store[job_id] = {"status": "downloading", "percent": 0, "speed": "", "eta": "", "filename": ""}

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
    out_tmpl    = opts.get("output_template")
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

    tmpl = out_tmpl if out_tmpl else "%(title)s.%(ext)s"
    output_template = os.path.join(DOWNLOAD_DIR, tmpl)
    has_ffmpeg = check_ffmpeg()

    cmd = ["yt-dlp",
           "--extractor-args", "youtube:player_client=web,default",
           "--no-check-certificate"]

    # ── FORMAT ──
    if mode == "mp3":
        if has_ffmpeg:
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", audio_q]
        else:
            cmd += ["-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"]
            progress_store[job_id]["warning"] = "ffmpeg nathi — audio as-is download thase. Install: pkg install ffmpeg"
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
            progress_store[job_id]["warning"] = "ffmpeg nathi — single-file mp4 (max ~720p). HD mate: pkg install ffmpeg"

    # ── OUTPUT ──
    cmd += ["-o", output_template]

    # ── PLAYLIST ──
    if not playlist:
        cmd += ["--no-playlist"]
    if pl_start:  cmd += ["--playlist-start", str(pl_start)]
    if pl_end:    cmd += ["--playlist-end",   str(pl_end)]
    if pl_items:  cmd += ["--playlist-items",  pl_items]

    # ── SUBTITLES ──
    if subtitles and mode != "mp3":
        cmd += ["--write-auto-sub", "--embed-subs", "--sub-langs", sub_langs or "en"]

    # ── THUMBNAIL ──
    if embed_thumb and has_ffmpeg:
        cmd += ["--embed-thumbnail"]

    # ── METADATA ──
    if add_meta and has_ffmpeg:
        cmd += ["--embed-metadata"]

    # ── SPONSORBLOCK ──
    if sponsorblk:
        if sb_action == "remove":
            cmd += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]
        elif sb_action == "mark":
            cmd += ["--sponsorblock-mark", "sponsor,selfpromo,interaction"]
        elif sb_action == "chapter":
            cmd += ["--sponsorblock-chapter", "sponsor,selfpromo,interaction"]

    # ── CHAPTERS ──
    if chapters:
        cmd += ["--split-chapters"]

    # ── MISC OPTIONS ──
    if no_overwr:   cmd += ["--no-overwrites"]
    if info_json:   cmd += ["--write-info-json"]
    if rate_limit:  cmd += ["--rate-limit", rate_limit]
    if conc_frags:  cmd += ["--concurrent-fragments", str(conc_frags)]
    if retries:     cmd += ["--retries", str(retries)]
    if proxy:       cmd += ["--proxy", proxy]
    if cookies:     cmd += ["--cookies", cookies]
    if geo:         cmd += ["--geo-bypass-country", geo]
    if age_limit:   cmd += ["--age-limit", str(age_limit)]
    if sleep_min and sleep_max:
        cmd += ["--sleep-interval", str(sleep_min), "--max-sleep-interval", str(sleep_max)]

    # ── CLIP / TRIM ──
    if clip_start or clip_end:
        section = "*"
        if clip_start: section += clip_start
        section += "-"
        if clip_end:   section += clip_end
        cmd += ["--download-sections", section]

    # ── EXTRA ARGS ──
    if extra_args:
        cmd += shlex.split(extra_args)

    cmd += ["--no-warnings", url]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )

        stderr_lines = []
        already_done = False

        def read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line.strip())

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            m = re.search(r'\[download\]\s+([\d.]+)%.*?at\s+([\S]+)\s+ETA\s+([\S]+)', line)
            if m:
                pct = float(m.group(1))
                progress_store[job_id]["percent"] = pct
                progress_store[job_id]["speed"] = m.group(2)
                progress_store[job_id]["eta"] = m.group(3)
                if pct >= 100:
                    already_done = True

            m2 = re.search(r'Destination:\s+(.+)', line)
            if m2:
                progress_store[job_id]["filename"] = os.path.basename(m2.group(1))

            if "has already been downloaded" in line:
                already_done = True
                fname = re.search(r'\[download\] (.+) has already', line)
                if fname:
                    progress_store[job_id]["filename"] = os.path.basename(fname.group(1))

            if any(x in line for x in ["Merging formats", "Merger", "[ffmpeg]", "Deleting original"]):
                progress_store[job_id]["status"] = "merging"
                progress_store[job_id]["percent"] = 99

            if "Destination:" in line and (".mp3" in line or ".mp4" in line or ".m4a" in line):
                progress_store[job_id]["filename"] = os.path.basename(line.split("Destination:")[-1].strip())
                already_done = True

        proc.wait()
        stderr_thread.join(timeout=2)

        if proc.returncode == 0 or already_done:
            progress_store[job_id]["status"] = "done"
            progress_store[job_id]["percent"] = 100
            # Find actual downloaded file for serving
            fname = progress_store[job_id].get("filename", "")
            if fname:
                fpath = os.path.join(DOWNLOAD_DIR, fname)
                if os.path.exists(fpath):
                    progress_store[job_id]["download_ready"] = True
                    progress_store[job_id]["serve_name"] = fname
        else:
            err_text = " ".join(stderr_lines[-3:]) if stderr_lines else "yt-dlp error thayo"
            if "WARNING" in err_text and "ERROR" not in err_text:
                progress_store[job_id]["status"] = "done"
                progress_store[job_id]["percent"] = 100
            else:
                progress_store[job_id]["status"] = "error"
                progress_store[job_id]["error"] = err_text[:200] if err_text else "Download fail thayo"

    except Exception as e:
        progress_store[job_id]["status"] = "error"
        progress_store[job_id]["error"] = str(e)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Quiet logs

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

    def send_static_file(self, filename, content_type):
        static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.exists(static_path):
            with open(static_path, "rb") as f:
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
            self.wfile.write(f"{filename} not found!".encode())

    def send_html_file(self):
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "YT_STUDIO.html")
        if os.path.exists(html_path):
            with open(html_path, "rb") as f:
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
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_html_file()

        elif path == "/sw.js":
            self.send_static_file("sw.js", "application/javascript")

        elif path == "/manifest.json":
            self.send_static_file("manifest.json", "application/manifest+json")

        elif path == "/api/status":
            installed, version = check_ytdlp()
            ffmpeg = check_ffmpeg()
            self.send_json({
                "installed": installed,
                "version": version,
                "ffmpeg": ffmpeg,
                "download_dir": DOWNLOAD_DIR
            })

        elif path == "/api/info":
            url = params.get("url", [""])[0]
            if not url:
                self.send_json({"error": "URL required"}, 400)
                return
            info = get_video_info(url)
            self.send_json(info)

        elif path == "/api/progress":
            job_id = params.get("id", [""])[0]
            if job_id in progress_store:
                self.send_json(progress_store[job_id])
            else:
                self.send_json({"status": "not_found"}, 404)

        elif path.startswith("/api/file/"):
            # Serve downloaded file to browser then delete from /tmp
            raw_name = path[len("/api/file/"):]
            filename = urllib.parse.unquote(raw_name)
            # Security: no path traversal
            filename = os.path.basename(filename)
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.exists(filepath):
                mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                size = os.path.getsize(filepath)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", size)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    with open(filepath, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    # Delete after serving (save /tmp space on Render)
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "File not found or already downloaded"}).encode())

        elif path == "/api/thumbnail":
            # Proxy thumbnail to avoid CORS/mixed-content issues
            thumb_url = params.get("url", [""])[0]
            if not thumb_url:
                self.send_response(400); self.end_headers(); return
            try:
                import urllib.request
                req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = resp.read()
                    ctype = resp.headers.get("Content-Type", "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", len(data))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(502); self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/download":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_json({"error": "Invalid request body"}, 400)
                return

            # ✅ FIX: HTML sends 'urls' (list), support both 'urls' and 'url'
            urls = body.get("urls") or []
            if not urls:
                single = body.get("url", "")
                if single:
                    urls = [single]

            if not urls:
                self.send_json({"error": "URL nai malyo"}, 400)
                return

            # For now download first URL (batch support via multiple job_ids can be added later)
            url = urls[0]

            job_id = str(int(time.time() * 1000))
            t = threading.Thread(target=download_video, args=(job_id, url, body), daemon=True)
            t.start()
            self.send_json({"job_id": job_id})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    installed, version = check_ytdlp()
    is_render = bool(os.environ.get("RENDER"))
    print("=" * 50)
    print("  YDROP — YT Studio Downloader")
    print("=" * 50)
    if not installed:
        print("\n⚠️  yt-dlp install nathi!")
        print("  Run karo: pip install yt-dlp")
        print("  Pachhi aa script phir chalavo.\n")
    else:
        print(f"\n✓  yt-dlp v{version} ready")

    print(f"✓  Downloads: {DOWNLOAD_DIR}")
    if is_render:
        print(f"\n  Render par deploy chhe — port {PORT}")
        print("  ⚠️  /tmp/downloads ephemeral chhe (restart par reset)")
    else:
        print(f"\n  Browser ma kholo: http://localhost:{PORT}")
        print("  Band karva: Ctrl+C")
    print("=" * 50)

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer band thayo.")
