import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import re
import os
import sys
import time
import threading

PORT = int(os.environ.get("PORT", 54321))
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(DIRECTORY, "saved_channels.json")
CACHE_FILE = os.path.join(DIRECTORY, "channel_cache.json")
cache_lock = threading.Lock()
db_lock = threading.Lock()

# In-memory cache
cache = {}
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}

# Preload video metadata map from saved_channels.json
VIDEO_METADATA_MAP = {}
def load_video_metadata():
    global VIDEO_METADATA_MAP
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                ch_list = raw if isinstance(raw, list) else raw.get("channels", [])
                for ch in ch_list:
                    for v in ch.get("videos", []):
                        if v.get("id"):
                            cur = VIDEO_METADATA_MAP.get(v["id"], {})
                            views = v.get("views") or cur.get("views", "")
                            u_date = v.get("uploadDate") or cur.get("uploadDate", "")
                            VIDEO_METADATA_MAP[v["id"]] = {"views": views, "uploadDate": u_date}
        except Exception as e:
            print("Error loading saved_channels:", e, flush=True)

load_video_metadata()

_save_timer = None
_save_timer_lock = threading.Lock()
_file_write_lock = threading.Lock()

def _flush_cache_worker(data_copy):
    with _file_write_lock:
        temp_file = CACHE_FILE + ".tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data_copy, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, CACHE_FILE)
        except Exception as e:
            print(f"Error saving cache file: {e}", flush=True)

def save_cache(delay=0.8):
    global _save_timer
    try:
        now = time.time()
        with cache_lock:
            # Auto-evict: remove channel caches older than 2 hours (7200s)
            expired_keys = [
                k for k, v in list(cache.items())
                if not k.startswith("vid_") and (now - v.get("timestamp", 0) > 7200)
            ]
            for k in expired_keys:
                cache.pop(k, None)

            # Cap channel caches: keep only 5 most recently accessed channels
            channel_keys = [k for k in list(cache.keys()) if not k.startswith("vid_")]
            if len(channel_keys) > 5:
                sorted_channels = sorted(
                    channel_keys,
                    key=lambda k: cache.get(k, {}).get("timestamp", 0),
                    reverse=True
                )
                for old_key in sorted_channels[5:]:
                    cache.pop(old_key, None)

            # Cap video metadata entries if any: keep at most 50
            vid_keys = [k for k in list(cache.keys()) if k.startswith("vid_")]
            if len(vid_keys) > 50:
                for old_vid in vid_keys[:-50]:
                    cache.pop(old_vid, None)

            data_copy = dict(cache)

        with _save_timer_lock:
            if _save_timer and _save_timer.is_alive():
                _save_timer.cancel()
            _save_timer = threading.Timer(delay, _flush_cache_worker, args=(data_copy,))
            _save_timer.daemon = True
            _save_timer.start()
    except Exception as e:
        print(f"Error scheduling save_cache: {e}", flush=True)

# Initialize clean empty DB if DB doesn't exist
if not os.path.exists(DB_FILE):
    default_channels = {"channels": [], "activeChannelId": ""}
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(default_channels, f, ensure_ascii=False, indent=2)


def search_channel_by_keyword(query):
    try:
        api_url = "https://www.youtube.com/youtubei/v1/search"
        payload = json.dumps({
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20260904.01.00",
                    "hl": "ko",
                    "gl": "KR"
                }
            },
            "query": query.strip(),
            "params": "EgIQAg%3D%3D"
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        }
        req = urllib.request.Request(api_url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))

        def find_channel_id(o):
            if isinstance(o, dict):
                if "channelRenderer" in o:
                    cid = o["channelRenderer"].get("channelId")
                    if cid:
                        return cid
                for v in o.values():
                    res = find_channel_id(v)
                    if res:
                        return res
            elif isinstance(o, list):
                for i in o:
                    res = find_channel_id(i)
                    if res:
                        return res
            return None

        return find_channel_id(resp_data)
    except Exception as e:
        print(f"[SEARCH] Error searching channel for '{query}': {e}", flush=True)
        return None

def check_item_members_only(item_obj, title=""):
    # 1. Check title keywords
    t_low = (title or "").lower()
    title_kws = [
        "[멤버십", "[맴버십", "(멤버십", "(맴버십",
        "멤버십", "맴버십",
        "[회원전용", "(회원전용", "[회원 전용", "(회원 전용",
        "멤버십 전용", "맴버십 전용", "회원 전용", "회원전용",
        "members only", "member-only", "members-only",
        "가입자 전용", "가입자전용"
    ]
    if any(kw in t_low for kw in title_kws):
        return True

    # 2. Deep scan serialized item JSON for YouTube membership badges and styles
    if isinstance(item_obj, (dict, list)):
        try:
            raw_json = json.dumps(item_obj, ensure_ascii=False)
            if title:
                raw_json = raw_json.replace(title, "")
            raw_low = raw_json.lower()

            membership_tokens = [
                "badge_style_type_members_only",
                "sponsorships",
                "members_only",
                "회원 전용",
                "회원전용",
                "members only",
                "member-only",
                "members-only",
                "가입자 전용",
                "가입자전용",
                "채널 회원",
                "채널에 가입하여",
                "join this channel to get access",
                "join this channel"
            ]
            if any(token in raw_low for token in membership_tokens):
                return True
        except Exception:
            pass

    return False

def search_youtube_channels(query, limit=8):
    if not query or not query.strip():
        return []
    try:
        api_url = "https://www.youtube.com/youtubei/v1/search"
        payload = json.dumps({
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20240901.01.00",
                    "hl": "ko",
                    "gl": "KR"
                }
            },
            "query": query.strip(),
            "params": "EgIQAg%3D%3D"
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        }
        req = urllib.request.Request(api_url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))

        results = []
        seen_ids = set()
        def extract_channels(o):
            if isinstance(o, dict):
                if "channelRenderer" in o:
                    cr = o["channelRenderer"]
                    cid = cr.get("channelId")
                    if cid and cid not in seen_ids:
                        seen_ids.add(cid)
                        title = cr.get("title", {}).get("simpleText") or (cr.get("title", {}).get("runs", [{}])[0].get("text", ""))
                        subs = cr.get("videoCountText", {}).get("simpleText") or cr.get("subscriberCountText", {}).get("simpleText", "")
                        s_text = cr.get("subscriberCountText", {}).get("simpleText", "")
                        handle = s_text if "@" in s_text else ""
                        if not handle:
                            handle = cr.get("canonicalBaseUrl", "")
                        thumbs = cr.get("thumbnail", {}).get("thumbnails", [])
                        thumb = thumbs[-1].get("url") if thumbs else ""
                        if thumb and thumb.startswith("//"):
                            thumb = "https:" + thumb
                        results.append({
                            "channelId": cid,
                            "title": title,
                            "subscribers": subs,
                            "handle": handle,
                            "thumbnail": thumb
                        })
                for v in o.values():
                    extract_channels(v)
            elif isinstance(o, list):
                for i in o:
                    extract_channels(i)

        extract_channels(resp_data)
        return results[:limit]
    except Exception as e:
        print(f"[SEARCH_CHANNELS] Error searching for '{query}': {e}", flush=True)
        return []

def fetch_youtube_channel(channel_input, force=False):
    channel_input = channel_input.strip()
    cache_key = channel_input.lower()
    now = time.time()
    with cache_lock:
        if not force and cache_key in cache:
            cached_entry = cache[cache_key]
            if now - cached_entry.get("timestamp", 0) < 3600:
                print(f"[CACHE HIT] Returning cached data for {channel_input}")
                return cached_entry["data"]

    channel_id = None
    if re.match(r"^UC[\w-]{22}$", channel_input):
        channel_id = channel_input
    elif "channel/UC" in channel_input:
        m = re.search(r"channel/(UC[\w-]{22})", channel_input)
        if m:
            channel_id = m.group(1)

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json"
    }

    if not channel_id:
        clean_name = channel_input.replace("https://www.youtube.com/", "").replace("http://www.youtube.com/", "").replace("@", "").split("/")[0].strip()
        channel_id = search_channel_by_keyword(clean_name)

    if not channel_id:
        try:
            ch_url = channel_input if (channel_input.startswith("http://") or channel_input.startswith("https://")) else f"https://www.youtube.com/@{channel_input}"
            html_req = urllib.request.Request(ch_url, headers={"User-Agent": headers["User-Agent"]})
            with urllib.request.urlopen(html_req, timeout=8) as h_resp:
                h_text = h_resp.read().decode("utf-8", errors="replace")
            m_cid = re.search(r'channelId":"(UC[\w-]{22})"', h_text) or re.search(r'externalId":"(UC[\w-]{22})"', h_text)
            if m_cid:
                channel_id = m_cid.group(1)
        except Exception as e:
            print(f"[FETCH_CHANNEL_ID_ERROR]: {e}", flush=True)

    if not channel_id:
        raise ValueError(f"채널 ID를 찾을 수 없습니다: {channel_input}")

    api_url = "https://www.youtube.com/youtubei/v1/browse"
    payload = json.dumps({
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20260904.01.00", "hl": "ko", "gl": "KR"}},
        "browseId": channel_id,
        "params": "EgZ2aWRlb3PyBgQKAjoA"
    }).encode("utf-8")

    req = urllib.request.Request(api_url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    channel_name = "유튜브 채널"
    subscribers = ""
    try:
        header = data.get("header", {})
        phr = header.get("pageHeaderRenderer", {})
        vm = phr.get("content", {}).get("pageHeaderViewModel", {})
        channel_name = vm.get("title", {}).get("dynamicTextViewModel", {}).get("text", {}).get("content", channel_name)
        rows = vm.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
        for r in rows:
            for p in r.get("metadataParts", []):
                t = p.get("text", {}).get("content", "")
                if "구독자" in t:
                    subscribers = t
                    break
        if not subscribers:
            c4 = header.get("c4TabbedHeaderRenderer", {})
            sub_count = c4.get("subscriberCountText", {})
            if isinstance(sub_count, dict):
                subscribers = sub_count.get("simpleText") or sub_count.get("runs", [{}])[0].get("text", "")
            if not channel_name or channel_name == "유튜브 채널":
                channel_name = c4.get("title", channel_name)
    except Exception:
        pass

    videos = []
    seen = set()
    token = None

    def extract_rich_items(obj):
        nonlocal token
        items = []
        def walk(o):
            nonlocal token
            if isinstance(o, dict):
                if "richItemRenderer" in o:
                    items.append(o["richItemRenderer"])
                if "continuationCommand" in o and not token:
                    token = o["continuationCommand"].get("token")
                for v in o.values(): walk(v)
            elif isinstance(o, list):
                for v in o: walk(v)
        walk(obj)
        return items

    tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
    videos_tab_content = None
    for t in tabs:
        tr = t.get("tabRenderer", {})
        if tr.get("selected"):
            videos_tab_content = tr.get("content", {})
            break

    if not videos_tab_content:
        def find_grid(o):
            nonlocal videos_tab_content
            if videos_tab_content: return
            if isinstance(o, dict):
                if "richGridRenderer" in o:
                    videos_tab_content = o["richGridRenderer"]
                    return
                for v in o.values(): find_grid(v)
            elif isinstance(o, list):
                for v in o: find_grid(v)
        find_grid(data)

    if not videos_tab_content:
        raise ValueError("동영상 목록을 불러올 수 없습니다.")

    items = extract_rich_items(videos_tab_content)

    def parse_item(item):
        lvm = item.get("content", {}).get("lockupViewModel", {})
        vid = lvm.get("contentId")
        title = ""
        meta = {}
        if vid:
            meta = lvm.get("metadata", {}).get("lockupMetadataViewModel", {})
            title = meta.get("title", {}).get("content", "")
        else:
            vr = item.get("content", {}).get("videoRenderer", {}) or item.get("videoRenderer", {})
            vid = vr.get("videoId")
            if vid:
                title = vr.get("title", {}).get("runs", [{}])[0].get("text", "") or vr.get("title", {}).get("simpleText", "")

        if not vid or vid in seen: return None
        seen.add(vid)

        duration = ""
        views = ""
        upload_date = ""
        try:
            m_rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
            for r in m_rows:
                for p in r.get("metadataParts", []):
                    t = p.get("text", {}).get("content", "")
                    if "조회수" in t: views = t
                    elif "전" in t or "." in t: upload_date = t
        except Exception: pass
        try:
            badges = lvm.get("contentImage", {}).get("thumbnailViewModel", {}).get("overlays", [])
            for b in badges:
                for subb in b.get("thumbnailBottomOverlayViewModel", {}).get("badges", []):
                    t = subb.get("thumbnailBadgeViewModel", {}).get("text", "")
                    if ":" in t: duration = t
        except Exception: pass

        is_members = check_item_members_only(item, title)

        return {"id": vid, "title": title, "duration": duration, "views": views, "uploadDate": upload_date, "isMembersOnly": is_members}


    for it in items:
        p = parse_item(it)
        if p: videos.append(p)

    page = 1
    max_videos = 500
    while token and len(videos) < max_videos and page < 20:
        page += 1
        payload_cont = json.dumps({
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20260904.01.00", "hl": "ko", "gl": "KR"}},
            "continuation": token
        }).encode("utf-8")
        req_cont = urllib.request.Request(api_url, data=payload_cont, headers=headers)
        try:
            with urllib.request.urlopen(req_cont, timeout=10) as resp:
                data_cont = json.loads(resp.read().decode("utf-8"))
        except Exception:
            break

        token = None
        cont_items = extract_rich_items(data_cont)
        prev_count = len(videos)
        for it in cont_items:
            p = parse_item(it)
            if p: videos.append(p)
        if len(videos) == prev_count:
            break

    for idx, v in enumerate(videos):
        v["originalIndex"] = idx + 1

    result_data = {
        "name": channel_name,
        "channelName": channel_name,
        "id": channel_id,
        "channelId": channel_id,
        "channelUrl": f"https://www.youtube.com/channel/{channel_id}/videos",
        "playlistId": f"UU{channel_id[2:]}",
        "subscribers": subscribers or "구독자 정보 없음",
        "videos": videos
    }

    with cache_lock:
        cache[cache_key] = {"timestamp": now, "data": result_data}
    with db_lock:
        for v in videos:
            if v.get("id"):
                VIDEO_METADATA_MAP[v["id"]] = {"views": v.get("views", ""), "uploadDate": v.get("uploadDate", "")}
    save_cache()
    return result_data


class PlayerHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if parsed.path == "/robots.txt":
            robots_file = os.path.join(DIRECTORY, "robots.txt")
            if os.path.exists(robots_file):
                with open(robots_file, "rb") as f:
                    content = f.read()
            else:
                content = b"User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path == "/sitemap.xml":
            sitemap_file = os.path.join(DIRECTORY, "sitemap.xml")
            if os.path.exists(sitemap_file):
                with open(sitemap_file, "rb") as f:
                    content = f.read()
            else:
                content = b'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n  <url>\n    <loc>https://youtube-binge-player.onrender.com/</loc>\n    <priority>1.0</priority>\n  </url>\n</urlset>'
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path == "/" or parsed.path == "":
            self.send_response(302)
            self.send_header("Location", "/youtube_player.html")
            self.end_headers()
            return

        if parsed.path == "/api/video_info":
            query = urllib.parse.parse_qs(parsed.query)
            vid = query.get("id", [""])[0]
            force = query.get("force", ["false"])[0].lower() == "true"
            if not vid:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "ID 누락"}, ensure_ascii=False).encode("utf-8"))
                return

            cache_k = f"vid_{vid}"
            with cache_lock:
                cached_item = cache.get(cache_k)
            if not force and cached_item:
                # If cached within last 10 minutes, return immediately
                if time.time() - cached_item.get("timestamp", 0) < 600:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "data": cached_item["data"]}, ensure_ascii=False).encode("utf-8"))
                    return

            # Real-time YouTube InnerTube next API fetch (takes ~0.3s, 100% live views & date)
            title_str = ""
            date_str = ""
            views_str = ""
            likes_str = ""
            subscribers_str = ""
            try:
                api_url = "https://www.youtube.com/youtubei/v1/next"
                payload = json.dumps({
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": "2.20260904.01.00",
                            "hl": "ko",
                            "gl": "KR"
                        }
                    },
                    "videoId": vid
                }).encode("utf-8")

                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                }
                req = urllib.request.Request(api_url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=4) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8", errors="replace"))

                results = resp_json.get("contents", {}).get("twoColumnWatchNextResults", {}).get("results", {}).get("results", {}).get("contents", [])
                primary = None
                secondary = None
                for c in results:
                    if "videoPrimaryInfoRenderer" in c:
                        primary = c["videoPrimaryInfoRenderer"]
                    elif "videoSecondaryInfoRenderer" in c:
                        secondary = c["videoSecondaryInfoRenderer"]

                if primary:
                    t_runs = primary.get("title", {}).get("runs", [{}])
                    title_str = t_runs[0].get("text", "") if t_runs else ""
                    date_raw = primary.get("dateText", {}).get("simpleText", "")
                    if date_raw:
                        pts = [p.strip().rstrip(".") for p in date_raw.split(".") if p.strip()]
                        if len(pts) == 3:
                            date_str = f"{pts[0]}. {pts[1].zfill(2)}. {pts[2].zfill(2)}."
                        else:
                            date_str = date_raw
                    vc = primary.get("viewCount", {}).get("videoViewCountRenderer", {})
                    views_str = vc.get("viewCount", {}).get("simpleText", "") or vc.get("shortViewCount", {}).get("simpleText", "")

                    for b in primary.get("videoActions", {}).get("menuRenderer", {}).get("topLevelButtons", []):
                        like_vm = b.get("segmentedLikeDislikeButtonViewModel", {}).get("likeButtonViewModel", {}).get("likeButtonViewModel", {})
                        t = like_vm.get("toggleButtonViewModel", {}).get("toggleButtonViewModel", {}).get("defaultButtonViewModel", {}).get("buttonViewModel", {}).get("title", "")
                        if t:
                            likes_str = t
                        if not likes_str:
                            tb = b.get("toggleButtonRenderer", {})
                            t2 = tb.get("defaultText", {}).get("simpleText", "") or tb.get("defaultText", {}).get("accessibility", {}).get("accessibilityData", {}).get("label", "")
                            if t2 and any(ch.isdigit() for ch in t2):
                                likes_str = t2

                if secondary:
                    owner = secondary.get("owner", {}).get("videoOwnerRenderer", {})
                    subscribers_str = owner.get("subscriberCountText", {}).get("simpleText", "")
            except Exception as e:
                pass

            # Fallback to preloaded metadata map if live fetch was empty
            with db_lock:
                pre = dict(VIDEO_METADATA_MAP.get(vid, {}))
            if not date_str:
                date_str = pre.get("uploadDate", "")
            if not views_str:
                views_str = pre.get("views", "")

            vdata = {
                "title": title_str,
                "views": views_str or "조회수 정보 없음",
                "uploadDate": date_str or "날짜 미상",
                "likes": likes_str,
                "subscribers": subscribers_str
            }
            with cache_lock:
                cache[cache_k] = {"timestamp": time.time(), "data": vdata}
            with db_lock:
                VIDEO_METADATA_MAP[vid] = vdata
            save_cache()

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "data": vdata}, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/sync_latest_videos":
            query = urllib.parse.parse_qs(parsed.query)
            channel_id = query.get("channel_id", [""])[0]
            if not channel_id:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "channel_id 누락"}, ensure_ascii=False).encode("utf-8"))
                return

            try:
                api_url = "https://www.youtube.com/youtubei/v1/browse"
                payload = json.dumps({
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": "2.20260904.01.00",
                            "hl": "ko",
                            "gl": "KR"
                        }
                    },
                    "browseId": channel_id,
                    "params": "EgZ2aWRlb3PyBgQKAjoA"
                }).encode("utf-8")

                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                }
                req = urllib.request.Request(api_url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=6) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))

                items = []
                subscribers = ""
                try:
                    header = resp_data.get("header", {})
                    phr = header.get("pageHeaderRenderer", {})
                    vm = phr.get("content", {}).get("pageHeaderViewModel", {})
                    rows = vm.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                    for r in rows:
                        for p in r.get("metadataParts", []):
                            t = p.get("text", {}).get("content", "")
                            if "구독자" in t:
                                subscribers = t
                                break
                except Exception:
                    pass

                def find_items(o):
                    if isinstance(o, dict):
                        if "lockupViewModel" in o:
                            items.append(o["lockupViewModel"])
                        for v in o.values():
                            find_items(v)
                    elif isinstance(o, list):
                        for i in o:
                            find_items(i)
                find_items(resp_data)

                latest_videos = []
                seen = set()
                for lvm in items:
                    vid = lvm.get("contentId")
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    meta = lvm.get("metadata", {}).get("lockupMetadataViewModel", {})
                    title = meta.get("title", {}).get("content", "")
                    duration = ""
                    views = ""
                    upload_date = ""
                    is_members = check_item_members_only(lvm, title)

                    if vid and title:
                        v_obj = {
                            "id": vid,
                            "title": title,
                            "duration": duration,
                            "views": views,
                            "uploadDate": upload_date,
                            "isMembersOnly": is_members
                        }
                        latest_videos.append(v_obj)
                        with db_lock:
                            VIDEO_METADATA_MAP[vid] = {"views": views, "uploadDate": upload_date}

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "subscribers": subscribers,
                    "latestVideos": latest_videos
                }, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/search_channels":
            query = urllib.parse.parse_qs(parsed.query)
            q = query.get("q", [""])[0]
            if not q:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "channels": []}, ensure_ascii=False).encode("utf-8"))
                return
            try:
                channels = search_youtube_channels(q)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "channels": channels}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/fetch_channel":
            query = urllib.parse.parse_qs(parsed.query)
            target_url = query.get("url", [""])[0]
            force = query.get("force", ["false"])[0].lower() == "true"
            if not target_url:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "URL이 누락되었습니다."}, ensure_ascii=False).encode("utf-8"))
                return

            try:
                result = fetch_youtube_channel(target_url, force=force)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "data": result}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/channels":
            try:
                with db_lock:
                    with open(DB_FILE, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                        if isinstance(raw, list):
                            channels = raw
                            active_id = ""
                        elif isinstance(raw, dict):
                            channels = raw.get("channels", [])
                            active_id = raw.get("activeChannelId", "")
                        else:
                            channels = []
                            active_id = ""

                # Normalize and deduplicate channels
                dedup = {}
                for ch in channels:
                    cid = ch.get("id") or ch.get("channelId")
                    cname = ch.get("name") or ch.get("channelName") or "채널"
                    ch["id"] = cid
                    ch["name"] = cname
                    if cid:
                        if cid in dedup:
                            if len(ch.get("videos", [])) > len(dedup[cid].get("videos", [])):
                                dedup[cid] = ch
                        else:
                            dedup[cid] = ch
                channels = list(dedup.values())
            except Exception:
                channels = []
                active_id = ""
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "channels": channels, "activeChannelId": active_id}, ensure_ascii=False).encode("utf-8"))
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/channels":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length > 0 else b'{}'
            try:
                data = json.loads(body.decode('utf-8'))
                # Normalize channels
                ch_list = data.get("channels", []) if isinstance(data, dict) else data
                for ch in ch_list:
                    ch["id"] = ch.get("id") or ch.get("channelId")
                    ch["name"] = ch.get("name") or ch.get("channelName") or "채널"

                with db_lock:
                    temp_db = DB_FILE + ".tmp"
                    with open(temp_db, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(temp_db, DB_FILE)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def run_server():
    with ThreadedTCPServer(("0.0.0.0", PORT), PlayerHandler) as httpd:
        print(f"Multi-threaded Server running on http://localhost:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")

if __name__ == "__main__":
    run_server()
