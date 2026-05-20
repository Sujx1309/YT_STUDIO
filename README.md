# YT Studio — Deployment Guide

## Files needed
```
server.py
YT_STUDIO.html
sw.js
manifest.json
requirements.txt
render.yaml
```

---

## 🖥️ Local / Termux
```bash
pip install yt-dlp
python server.py
# Open: http://localhost:8765
```

---

## ☁️ Render (Free hosting)

1. Push all files to a **GitHub repo**
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — just click **Deploy**
5. Your app URL: `https://yt-studio.onrender.com` (or similar)

> ⚠️ **Note:** Render free tier uses `/tmp/downloads` which resets on restart.
> Files are temporary — download them immediately after they're ready.

---

## 📦 GitHub Pages note

GitHub Pages is **static only** — it cannot run Python.
Use Render (above) for the backend. You can host just the HTML on Pages
as a frontend that points to your Render backend URL if needed.

---

## 📱 PWA Install

Once the app is open in Chrome/Safari:
- **Android**: Tap the "Install" button in the header, or use Chrome menu → "Add to Home Screen"
- **iOS**: Safari → Share → "Add to Home Screen"
