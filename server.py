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
from datetime import datetime

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

def parse_youtube_date_to_days(date_str):
    if not date_str:
        return 999999.0
    s = str(date_str).replace("스트리밍 시간:", "").replace("최초 공개:", "").replace("스트리밍됨", "").strip()
    m_abs = re.search(r"(\d{4})[.-]\s*(\d{1,2})[.-]\s*(\d{1,2})", s)
    if m_abs:
        try:
            d = datetime(int(m_abs.group(1)), int(m_abs.group(2)), int(m_abs.group(3)))
            return (datetime.now() - d).total_seconds() / 86400.0
        except Exception:
            pass
    m = re.search(r"(\d+)\s*(초|분|시간|일|주|개월|달|년)\s*전", s)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == "초": return val / 86400.0
        if unit == "분": return val / 1440.0
        if unit == "시간": return val / 24.0
        if unit == "일": return float(val)
        if unit == "주": return float(val * 7)
        if unit in ("개월", "달"): return float(val * 30.5)
        if unit == "년": return float(val * 365.0)
    m_en = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago", s, re.IGNORECASE)
    if m_en:
        val = int(m_en.group(1))
        unit = m_en.group(2).lower()
        if unit == "second": return val / 86400.0
        if unit == "minute": return val / 1440.0
        if unit == "hour": return val / 24.0
        if unit == "day": return float(val)
        if unit == "week": return float(val * 7)
        if unit == "month": return float(val * 30.5)
        if unit == "year": return float(val * 365.0)
    return 999999.0

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
            encoded_url = urllib.parse.quote(ch_url, safe=":/?#[]@!$&'()*+,;=")
            html_req = urllib.request.Request(encoded_url, headers={"User-Agent": headers["User-Agent"]})
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
    channel_name = "유튜브 채널"
    subscribers = ""
    seen = set()
    videos = []
    start_total_time = time.time()

    def parse_item(item):
        lvm = item.get("content", {}).get("lockupViewModel", {}) or item.get("lockupViewModel", {})
        vr = (
            item.get("content", {}).get("videoRenderer", {})
            or item.get("videoRenderer", {})
            or item.get("gridVideoRenderer", {})
            or item.get("playlistVideoRenderer", {})
        )
        vid = lvm.get("contentId") or vr.get("videoId")
        if not vid or vid in seen:
            return None

        title = ""
        duration = ""
        views = ""
        upload_date = ""

        if lvm.get("contentId"):
            meta = lvm.get("metadata", {}).get("lockupMetadataViewModel", {})
            title = meta.get("title", {}).get("content", "")
            if not title and "runs" in meta.get("title", {}):
                title = "".join(r.get("text", "") for r in meta.get("title", {}).get("runs", []))
            try:
                m_rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                for r in m_rows:
                    for p in r.get("metadataParts", []):
                        t = p.get("text", {}).get("content", "")
                        if "조회수" in t:
                            views = t
                        elif "전" in t or "." in t:
                            upload_date = t
            except Exception:
                pass
            try:
                badges = lvm.get("contentImage", {}).get("thumbnailViewModel", {}).get("overlays", [])
                for b in badges:
                    for subb in b.get("thumbnailBottomOverlayViewModel", {}).get("badges", []):
                        t = subb.get("thumbnailBadgeViewModel", {}).get("text", "")
                        if ":" in t:
                            duration = t
            except Exception:
                pass
        else:
            title = (
                "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", []))
                or vr.get("title", {}).get("simpleText", "")
            )
            duration = vr.get("lengthText", {}).get("simpleText", "")
            if not duration:
                for ov in vr.get("thumbnailOverlays", []):
                    t_rend = ov.get("thumbnailOverlayTimeStatusRenderer", {})
                    t = t_rend.get("text", {}).get("simpleText", "") or "".join(r.get("text", "") for r in t_rend.get("text", {}).get("runs", []))
                    if ":" in t:
                        duration = t
                        break
            views = vr.get("viewCountText", {}).get("simpleText", "") or vr.get("shortViewCountText", {}).get("simpleText", "")
            if not views and "runs" in vr.get("viewCountText", {}):
                views = "".join(r.get("text", "") for r in vr.get("viewCountText", {}).get("runs", []))
            upload_date = vr.get("publishedTimeText", {}).get("simpleText", "")
            if not upload_date and "runs" in vr.get("publishedTimeText", {}):
                upload_date = "".join(r.get("text", "") for r in vr.get("publishedTimeText", {}).get("runs", []))

        seen.add(vid)
        is_members = check_item_members_only(item, title)

        return {
            "id": vid,
            "title": title or "제목 없음",
            "duration": duration,
            "views": views,
            "uploadDate": upload_date,
            "isMembersOnly": is_members
        }

    def fetch_tab_videos(tab_params, max_tab_videos=3000, max_pages=80, time_budget=30):
        nonlocal channel_name, subscribers
        tab_vids = []
        token = None

        def extract_items(obj):
            nonlocal token
            items = []
            def walk(o):
                nonlocal token
                if isinstance(o, dict):
                    if "richItemRenderer" in o:
                        items.append(o["richItemRenderer"])
                    elif "videoRenderer" in o and "richItemRenderer" not in o:
                        items.append(o)
                    elif "gridVideoRenderer" in o:
                        items.append(o)
                    elif "playlistVideoRenderer" in o:
                        items.append(o)

                    if "continuationCommand" in o and not token:
                        token = o["continuationCommand"].get("token")
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(obj)
            return items

        payload = json.dumps({
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20260904.01.00", "hl": "ko", "gl": "KR"}},
            "browseId": channel_id,
            "params": tab_params
        }).encode("utf-8")

        req = urllib.request.Request(api_url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[FETCH_TAB_ERROR] params={tab_params}: {e}", flush=True)
            return tab_vids

        try:
            header = data.get("header", {})
            phr = header.get("pageHeaderRenderer", {})
            vm = phr.get("content", {}).get("pageHeaderViewModel", {})
            c_name = vm.get("title", {}).get("dynamicTextViewModel", {}).get("text", {}).get("content", "")
            if c_name:
                channel_name = c_name
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

        tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
        tab_content = None
        for t in tabs:
            tr = t.get("tabRenderer", {})
            if tr.get("selected"):
                tab_content = tr.get("content", {})
                break

        if not tab_content:
            def find_grid(o):
                nonlocal tab_content
                if tab_content:
                    return
                if isinstance(o, dict):
                    if "richGridRenderer" in o:
                        tab_content = o["richGridRenderer"]
                        return
                    for v in o.values():
                        find_grid(v)
                elif isinstance(o, list):
                    for v in o:
                        find_grid(v)
            find_grid(data)

        if not tab_content:
            return tab_vids

        items = extract_items(tab_content)
        for it in items:
            p = parse_item(it)
            if p:
                tab_vids.append(p)

        page = 1
        t_tab_start = time.time()
        while token and len(tab_vids) < max_tab_videos and page < max_pages:
            if time.time() - t_tab_start > time_budget:
                break
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
            cont_items = extract_items(data_cont)
            prev_cnt = len(tab_vids)
            for it in cont_items:
                p = parse_item(it)
                if p:
                    tab_vids.append(p)
            if len(tab_vids) == prev_cnt:
                break

        return tab_vids

    # 1. Fetch "동영상" (Videos) tab: EgZ2aWRlb3PyBgQKAjoA (Max 15s budget)
    videos_tab_list = fetch_tab_videos("EgZ2aWRlb3PyBgQKAjoA", max_tab_videos=3000, max_pages=80, time_budget=15)
    videos.extend(videos_tab_list)

    # 2. Fetch "라이브" (Streams / Replays) tab: EgdzdHJlYW1z8gYECgJ6AA== (Stay well under Cloudflare 30s timeout)
    elapsed = time.time() - start_total_time
    if elapsed < 16 or len(videos) == 0:
        remain_budget = max(4, min(6, 21 - int(elapsed)))
        streams_tab_list = fetch_tab_videos("EgdzdHJlYW1z8gYECgJ6AA==", max_tab_videos=1000, max_pages=30, time_budget=remain_budget)
        videos.extend(streams_tab_list)

    if not videos:
        raise ValueError("동영상 목록을 불러올 수 없습니다.")

    # Sort videos: newest first by upload / streaming date
    videos.sort(key=lambda x: parse_youtube_date_to_days(x.get("uploadDate", "")))

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
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                }

                subscribers = ""
                raw_items = []

                def fetch_sync_tab(tab_params):
                    nonlocal subscribers
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
                        "params": tab_params
                    }).encode("utf-8")
                    req = urllib.request.Request(api_url, data=payload, headers=headers)
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))

                    if not subscribers:
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

                    tab_items = []
                    def find_items(o):
                        if isinstance(o, dict):
                            if "richItemRenderer" in o:
                                tab_items.append(o["richItemRenderer"])
                            elif "videoRenderer" in o and "richItemRenderer" not in o:
                                tab_items.append(o)
                            for v in o.values():
                                find_items(v)
                        elif isinstance(o, list):
                            for i in o:
                                find_items(i)
                    find_items(resp_data)
                    return tab_items

                try:
                    raw_items = fetch_sync_tab("EgZ2aWRlb3PyBgQKAjoA")
                except Exception:
                    raw_items = []

                try:
                    stream_items = fetch_sync_tab("EgdzdHJlYW1z8gYECgJ6AA==")
                    raw_items.extend(stream_items[:15])
                except Exception:
                    pass

                latest_videos = []
                seen = set()
                for it in raw_items:
                    lvm = it.get("content", {}).get("lockupViewModel", {}) or it.get("lockupViewModel", {})
                    vr = it.get("content", {}).get("videoRenderer", {}) or it.get("videoRenderer", {})
                    vid = lvm.get("contentId") or vr.get("videoId")
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)

                    title = ""
                    duration = ""
                    views = ""
                    upload_date = ""

                    if lvm.get("contentId"):
                        meta = lvm.get("metadata", {}).get("lockupMetadataViewModel", {})
                        title = meta.get("title", {}).get("content", "")
                        if not title and "runs" in meta.get("title", {}):
                            title = "".join(r.get("text", "") for r in meta.get("title", {}).get("runs", []))
                        try:
                            m_rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                            for r in m_rows:
                                for p in r.get("metadataParts", []):
                                    t = p.get("text", {}).get("content", "")
                                    if "조회수" in t:
                                        views = t
                                    elif "전" in t or "." in t:
                                        upload_date = t
                        except Exception:
                            pass
                        try:
                            badges = lvm.get("contentImage", {}).get("thumbnailViewModel", {}).get("overlays", [])
                            for b in badges:
                                for subb in b.get("thumbnailBottomOverlayViewModel", {}).get("badges", []):
                                    t = subb.get("thumbnailBadgeViewModel", {}).get("text", "")
                                    if ":" in t:
                                        duration = t
                        except Exception:
                            pass
                    else:
                        title = "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", [])) or vr.get("title", {}).get("simpleText", "")
                        duration = vr.get("lengthText", {}).get("simpleText", "")
                        if not duration:
                            for ov in vr.get("thumbnailOverlays", []):
                                t_rend = ov.get("thumbnailOverlayTimeStatusRenderer", {})
                                t = t_rend.get("text", {}).get("simpleText", "") or "".join(r.get("text", "") for r in t_rend.get("text", {}).get("runs", []))
                                if ":" in t:
                                    duration = t
                                    break
                        views = vr.get("viewCountText", {}).get("simpleText", "") or vr.get("shortViewCountText", {}).get("simpleText", "")
                        if not views and "runs" in vr.get("viewCountText", {}):
                            views = "".join(r.get("text", "") for r in vr.get("viewCountText", {}).get("runs", []))
                        upload_date = vr.get("publishedTimeText", {}).get("simpleText", "")
                        if not upload_date and "runs" in vr.get("publishedTimeText", {}):
                            upload_date = "".join(r.get("text", "") for r in vr.get("publishedTimeText", {}).get("runs", []))

                    is_members = check_item_members_only(it, title)

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

                latest_videos.sort(key=lambda x: parse_youtube_date_to_days(x.get("uploadDate", "")))

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "subscribers": subscribers,
                    "latestVideos": latest_videos[:25]
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
