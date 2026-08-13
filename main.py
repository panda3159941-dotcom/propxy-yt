"""
Search-only proxy for Epotify.

Deployed on Render.com (or any host with unblocked outbound access to
YouTube/YouTube Music). Exposes ONE endpoint, /search, which returns track
METADATA only (title, artist, artwork, duration, video id) - never audio.

The main HF Space backend calls this instead of talking to YouTube directly,
which fixes the "SSL: UNEXPECTED_EOF_WHILE_READING" errors caused by HF
Spaces' outbound network being blocked/throttled by YouTube.

This service intentionally does NOT proxy or return audio/stream URLs.
"""

import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("search_proxy")
logging.basicConfig(level=logging.INFO)

try:
    from ytmusicapi import YTMusic
except Exception as e:
    logger.warning("ytmusicapi import failed: %r", e)
    YTMusic = None

try:
    from youtubesearchpython import VideosSearch
except Exception as e:
    logger.warning("youtubesearchpython import failed: %r", e)
    VideosSearch = None

try:
    from yt_dlp import YoutubeDL
except Exception as e:
    logger.warning("yt-dlp import failed: %r", e)
    YoutubeDL = None

logger.info(
    "Search backends available: ytmusicapi=%s youtubesearchpython=%s yt_dlp=%s",
    YTMusic is not None, VideosSearch is not None, YoutubeDL is not None,
)

# Simple shared-secret so random people can't hammer your Render service.
PROXY_TOKEN = os.environ.get("PROXY_TOKEN")

app = FastAPI(title="Epotify Search Proxy")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_ytmusic():
    if YTMusic is None:
        return None
    try:
        return YTMusic()
    except Exception as e:
        logger.warning("YTMusic() init failed: %r", e)
        return None


def _parse_dur(t):
    try:
        parts = [int(p) for p in str(t).split(":")]
        if len(parts) == 3:
            h, m, s = parts
            return (h * 3600 + m * 60 + s) * 1000
        if len(parts) == 2:
            m, s = parts
            return (m * 60 + s) * 1000
    except Exception:
        pass
    return 30000


@app.get("/health")
def health():
    return {
        "ytmusicapi": YTMusic is not None,
        "youtubesearchpython": VideosSearch is not None,
        "yt_dlp": YoutubeDL is not None,
    }


@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"status": "Alive!"}


@app.get("/search")
def search(q: str, limit: int = 25, token: str | None = None):
    if PROXY_TOKEN and token != PROXY_TOKEN:
        raise HTTPException(403, "Invalid proxy token")

    q = q.strip()
    if not q:
        return []

    tracks = []

    client = _get_ytmusic()
    if client is not None:
        try:
            results = client.search(query=q, filter="songs", limit=limit)
        except Exception as e:
            logger.warning("ytmusicapi search failed for %r: %r", q, e)
            results = []
        for item in results:
            vid = item.get("videoId") or item.get("browseId")
            if not vid:
                continue
            title = item.get("title") or item.get("name") or "Без названия"
            artists = item.get("artists") or []
            artist = (
                artists[0]["name"]
                if artists and isinstance(artists[0], dict) and "name" in artists[0]
                else (artists[0] if artists else "Неизвестен")
            )
            thumbs = item.get("thumbnails") or []
            artwork = thumbs[-1]["url"] if thumbs else ""
            duration_text = item.get("duration") or item.get("duration_text") or "0:30"
            tracks.append({
                "track_id": vid,
                "title": title,
                "artist": artist,
                "album": item.get("album") or "",
                "artwork": artwork,
                "duration_ms": _parse_dur(duration_text),
                "video_id": vid,
            })

    if not tracks and VideosSearch is not None:
        try:
            v = VideosSearch(q, limit=limit)
            res = v.result()
            for item in res.get("result", []):
                vid = item.get("id") or item.get("link", "").split("v=")[-1]
                thumbs = item.get("thumbnails") or []
                channel = item.get("channel")
                tracks.append({
                    "track_id": vid,
                    "title": item.get("title") or "Без названия",
                    "artist": channel.get("name") if isinstance(channel, dict) else "Неизвестен",
                    "album": "",
                    "artwork": thumbs[0]["url"] if thumbs else "",
                    "duration_ms": _parse_dur(item.get("duration") or "0:30"),
                    "video_id": vid,
                })
        except Exception as e:
            logger.warning("youtubesearchpython failed for %r: %r", q, e)

    if not tracks and YoutubeDL is not None:
        try:
            ydl_opts = {"quiet": True, "skip_download": True, "extract_flat": True}
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
                for item in info.get("entries", []) or []:
                    vid = item.get("id") or ""
                    if not vid:
                        continue
                    duration_sec = item.get("duration") or 0
                    duration_ms = (
                        int(duration_sec * 1000)
                        if isinstance(duration_sec, (int, float))
                        else 30000
                    )
                    artwork = item.get("thumbnail") or ""
                    if not artwork:
                        thumbs = item.get("thumbnails") or []
                        if thumbs:
                            artwork = thumbs[-1].get("url") or ""
                    tracks.append({
                        "track_id": vid,
                        "title": item.get("title") or "Без названия",
                        "artist": item.get("uploader") or "Неизвестен",
                        "album": "",
                        "artwork": artwork,
                        "duration_ms": duration_ms,
                        "video_id": vid,
                    })
        except Exception as e:
            logger.warning("yt-dlp ytsearch failed for %r: %r", q, e)

    if not tracks:
        logger.warning("Search %r returned 0 results from all sources", q)

    for t in tracks:
        if not t.get("artwork") and t.get("video_id"):
            t["artwork"] = f"https://i.ytimg.com/vi/{t['video_id']}/hqdefault.jpg"

    return tracks
