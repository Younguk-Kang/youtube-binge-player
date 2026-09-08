# 🎬 YouTube Binge Player

> A lightweight, web-based YouTube binge-watching player designed for seamless chronological watching from episode 1 to the latest.

---

## ✨ Key Features

- 🔄 **Chronological (Oldest First) & Reverse (Newest First) Modes**
  - Instant list order toggle without interrupting ongoing video playback.
  - Automatically plays sequentially from the very first video in binge mode.
- 🔒 **Membership (Members-Only) Video Filter**
  - One-click toggle (`[🔒 Exclude Membership]` / `[🔓 Include Membership]`) to filter out inaccessible paid-only videos.
- 🔍 **Channel Search & Instant Video Scraping**
  - Search any channel keyword or paste a channel URL to fetch hundreds of videos in less than 0.5s.
- ⏱️ **Second-Accurate Resume Playback**
  - Remembers the exact second and video where you left off, even across browser restarts.
- ✓ **Watched History & Auto Play Next**
  - Automatically tracks watched episodes with checkmarks and transitions smoothly to the next video upon completion.
- 🖥️ **Flexible Display Modes (Normal & Theater / Expanded)**
  - Switch between standard view (video + playlist side-by-side) and theater view (maximized video focus).
- ⚡ **Zero-Overhead & Ultra-Lightweight Architecture**
  - Video streaming runs directly between YouTube CDN and the browser (0% server bandwidth consumption).
  - Built strictly with Python standard libraries — zero external dependencies needed.

---

## 🚀 Quick Start (Local Run)

Requires only **Python 3** (no external pip packages needed):

```bash
python server.py
```

Then open your browser and navigate to:
```
http://localhost:54321
```

---

## 🌐 1-Click Free Cloud Deployment (e.g. Render.com)

Easily deploy to free cloud hosting platforms such as [Render.com](https://render.com) in under a minute:

1. Push this repository to your GitHub account.
2. Sign in to [Render.com](https://render.com) and click **New + > Web Service**.
3. Select and connect your `youtube-binge-player` GitHub repository.
4. Verify deployment configuration:
   - **Environment**: `Python`
   - **Build Command**: `pip install -r requirements.txt` (or leave empty)
   - **Start Command**: `python server.py`
5. Click **Create Web Service**. Your free HTTPS live URL (`https://<project-name>.onrender.com`) will be up in seconds!

---

## 📄 License
MIT License. Open source and free to customize.
