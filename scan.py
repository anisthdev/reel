#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["guessit"]
# ///
"""Scan video folders into the library: ~/.cache/reel/library.json, which
mpv's start screen (and anything else) reads.

Rows come from ~/.config/reel/library.toml (see there), each one either
  type = "movies"   one title per file or folder
  type = "series"   one show per folder; folders with the same title
                    (e.g. separate season folders) are merged

Writes ~/.cache/reel/library.json plus BGRA thumbnails ready for
mpv's overlay-add, and the JPEGs they came from (poster, backdrop, still). Posters, details and episode stills come from TMDB when
online (token in ~/.config/reel/tmdb_token or $TMDB_TOKEN); anything
missing falls back to a frame grabbed from the video.
"""

import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


VIDEOS = Path("~/Videos").expanduser()
ROWS_FILE = Path("~/.config/reel/library.toml").expanduser()
DEFAULT_ROWS = [
    {"name": "Movies", "path": str(VIDEOS / "Movies"), "type": "movies"},
    {"name": "TV", "path": str(VIDEOS / "TV"), "type": "series"},
    {"name": "Anime", "path": str(VIDEOS / "Anime"), "type": "series"},
]
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".webm", ".mov"}
SKIP_DIRS = {"extras", "extra", "featurettes", "specials", "sample", "samples"}

CONFIG = Path("~/.config/reel").expanduser()
OVERRIDES = CONFIG / "overrides.json"
CACHE = Path("~/.cache/reel").expanduser()
TMDB_CACHE = CACHE / "tmdb"
IMAGES = CACHE / "images"
THUMBS = CACHE / "thumbs"
PROBE_CACHE = CACHE / "probe.json"
GUESS_CACHE = CACHE / "guess.json"
LIBRARY = CACHE / "library.json"

POSTER_SIZE = (220, 330)
CARD_SIZE = (400, 225)
STILL_SIZE = (320, 180)
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p"
TMDB_TTL = 30 * 24 * 3600
# Hosts some ISPs block via DNS; resolved through DNS-over-HTTPS instead.
DOH_HOSTS = {"api.themoviedb.org"}


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def normalize(text):
    text = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


# guessit is ~20 ms per name and the names never change, so results are cached
# by name and guessit is only imported when there's something new to parse.
GUESS_VERSION = 2
_guess_cache = None


def guess(name):
    global _guess_cache
    if _guess_cache is None:
        try:
            data = json.loads(GUESS_CACHE.read_text())
            _guess_cache = data["names"] if data.get("version") == GUESS_VERSION else {}
        except (OSError, ValueError, KeyError):
            _guess_cache = {}
    if name not in _guess_cache:
        from guessit import guessit

        g = guessit(name)
        _guess_cache[name] = {
            "type": g.get("type"),
            "title": str(g["title"]) if g.get("title") else None,
            "year": first_int(g.get("year")),
            "season": first_int(g.get("season")),
            "episode": first_int(g.get("episode")),
            "episode_title": str(g["episode_title"]) if g.get("episode_title") else None,
        }
    return _guess_cache[name]


def save_guess_cache():
    if _guess_cache is not None:
        GUESS_CACHE.write_text(json.dumps({"version": GUESS_VERSION, "names": _guess_cache}))


def first_int(value):
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- DNS workaround ---------------------------------------------------------

_doh_ips = {}
_doh_index = {}
_system_getaddrinfo = socket.getaddrinfo


def doh_resolve(host):
    if host not in _doh_ips:
        url = "https://cloudflare-dns.com/dns-query?" + urllib.parse.urlencode({"name": host, "type": "A"})
        req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            answers = json.load(resp).get("Answer", [])
        _doh_ips[host] = sorted(a["data"] for a in answers if a.get("type") == 1)
        _doh_index[host] = 0
    return _doh_ips[host]


def doh_next_ip(host):
    """Skip to the next address after a failure; False once all were tried."""
    if not _doh_ips.get(host):
        return False
    _doh_index[host] += 1
    return _doh_index[host] < len(_doh_ips[host])


def getaddrinfo(host, *args, **kwargs):
    if host in DOH_HOSTS:
        try:
            ips = doh_resolve(host)
            if ips:
                ip = ips[_doh_index[host] % len(ips)]
                return _system_getaddrinfo(ip, *args, **kwargs)
        except OSError as exc:
            log(f"DoH lookup for {host} failed ({exc}), using system DNS")
    return _system_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = getaddrinfo


# --- TMDB -------------------------------------------------------------------


class Tmdb:
    def __init__(self):
        token = os.environ.get("TMDB_TOKEN")
        token_file = CONFIG / "tmdb_token"
        if not token and token_file.exists():
            token = token_file.read_text().strip()
        self.token = token
        self.online = bool(token)
        TMDB_CACHE.mkdir(parents=True, exist_ok=True)

    def get(self, path, **params):
        url = f"{TMDB_API}{path}?" + urllib.parse.urlencode(sorted(params.items()))
        cache_file = TMDB_CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".json")
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < TMDB_TTL:
            return json.loads(cache_file.read_text())
        if not self.online:
            return json.loads(cache_file.read_text()) if cache_file.exists() else None
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        while True:
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                raise
            except OSError as exc:
                # Some ISPs reset connections to individual TMDB addresses; try the others.
                if doh_next_ip("api.themoviedb.org"):
                    continue
                log(f"TMDB unreachable ({exc}); continuing offline")
                self.online = False
                return json.loads(cache_file.read_text()) if cache_file.exists() else None
        cache_file.write_text(json.dumps(data))
        return data

    def backdrop(self, kind, tmdb_id, details):
        """Prefer an English backdrop with the title on it; else the default one."""
        images = self.get(f"/{kind}/{tmdb_id}/images", include_image_language="en,null") or {}
        backdrops = images.get("backdrops") or []
        titled = [b for b in backdrops if b.get("iso_639_1") == "en"]
        if titled:
            return max(titled, key=lambda b: (b.get("vote_average") or 0, b.get("width") or 0))["file_path"]
        return (details or {}).get("backdrop_path")

    def search(self, kind, title, year=None):
        params = {"query": title, "include_adult": "false"}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        data = self.get(f"/search/{kind}", **params)
        results = (data or {}).get("results") or []
        if not results and year:
            return self.search(kind, title)
        if not results:
            return None
        wanted = normalize(title)
        name_key = "title" if kind == "movie" else "name"
        exact = [r for r in results if wanted in (normalize(r.get(name_key)), normalize(r.get("original_" + name_key)))]
        return (exact or results)[0]


# --- local files ------------------------------------------------------------


def video_files(path):
    if path.is_file():
        return [path] if path.suffix.lower() in VIDEO_EXTS else []
    files = []
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d.casefold() not in SKIP_DIRS]
        files += [Path(root) / n for n in names if Path(n).suffix.lower() in VIDEO_EXTS]
    return sorted(files)


def season_from_parents(path, stop):
    for parent in path.parents:
        if parent == stop.parent:
            break
        season = first_int(guess(parent.name)["season"])
        if season is not None:
            return season
    return None


def watch_later_name(path):
    # mpv names watch_later files after the MD5 of the path it was given.
    return hashlib.md5(str(path).encode()).hexdigest().upper()


def load_probe_cache():
    try:
        return json.loads(PROBE_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def probe(path, cache):
    stat = path.stat()
    key = str(path)
    stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    hit = cache.get(key)
    if hit and hit.get("stamp") == stamp and "width" in hit:
        return hit
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    info = {"stamp": stamp}
    try:
        data = json.loads(out.stdout)
        info["duration"] = round(float(data["format"]["duration"]), 1)
        video = next((s for s in data["streams"] if s.get("codec_type") == "video"), {})
        info["width"] = video.get("width")
        info["height"] = video.get("height")
        info["audio"] = sorted({s.get("tags", {}).get("language", "und") for s in data["streams"] if s.get("codec_type") == "audio"})
        info["subs"] = sorted({s.get("tags", {}).get("language", "und") for s in data["streams"] if s.get("codec_type") == "subtitle"})
    except (ValueError, KeyError, TypeError):
        pass
    cache[key] = info
    return info


# --- images -----------------------------------------------------------------


def thumb_path(key, size, source):
    # Separate names per source, so a frame grabbed while offline gets
    # replaced by the TMDB image on the next online scan.
    return THUMBS / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}_{size[0]}x{size[1]}_{source}.bgra"


def to_bgra(source, dest, size):
    w, h = size
    tmp = dest.with_suffix(".tmp")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source),
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgra", str(tmp)],
        capture_output=True,
    )
    if result.returncode == 0 and tmp.exists() and tmp.stat().st_size == w * h * 4:
        tmp.replace(dest)
        return True
    tmp.unlink(missing_ok=True)
    return False


def download(url, dest):
    if dest.exists():
        return True
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            dest.write_bytes(resp.read())
        return True
    except OSError:
        return False


def make_thumb(job):
    """job: (key, size, tmdb_image_path or None, video path for fallback, seek fraction)

    Returns (key, BGRA thumbnail, source, image): the image is the JPEG it was
    made from, for front ends that draw images themselves (quickbar).
    """
    key, size, image_path, video, fraction = job
    if image_path:
        dest = thumb_path(key, size, "tmdb")
        width = {POSTER_SIZE: "w342", CARD_SIZE: "w780"}.get(size, "w300")
        original = IMAGES / (width + image_path.replace("/", "_"))
        if dest.exists() and original.exists():
            return key, str(dest), "tmdb", str(original)
        if download(f"{TMDB_IMAGE}/{width}{image_path}", original) and (dest.exists() or to_bgra(original, dest, size)):
            return key, str(dest), "tmdb", str(original)
    if video:
        dest = thumb_path(key, size, "frame")
        frame = IMAGES / (dest.stem + ".jpg")
        if dest.exists() and frame.exists():
            return key, str(dest), "frame", str(frame)
        result = subprocess.run(
            ["ffmpegthumbnailer", "-i", str(video), "-o", str(frame), "-s", str(max(size) * 2), "-t", f"{int(fraction * 100)}%", "-q", "8"],
            capture_output=True,
        )
        if result.returncode == 0 and frame.exists() and to_bgra(frame, dest, size):
            return key, str(dest), "frame", str(frame)
    return key, None, "missing", None


# --- library ----------------------------------------------------------------


def load_rows():
    try:
        rows = tomllib.loads(ROWS_FILE.read_text()).get("rows") or []
    except FileNotFoundError:
        rows = DEFAULT_ROWS
    result = []
    for row in rows:
        paths = row.get("path") or []
        paths = [paths] if isinstance(paths, str) else paths
        if not row.get("name") or row.get("type") not in ("movies", "series"):
            log(f"Skipping row without name or with an unknown type: {row}")
            continue
        result.append({
            "name": row["name"],
            "type": row["type"],
            "paths": [Path(p).expanduser() for p in paths],
            "tmdb": row.get("tmdb", True),
            "sort": row.get("sort"),
        })
    return result


def row_entries(row):
    for root in row["paths"]:
        if root.exists():
            yield from sorted(root.iterdir())


def load_overrides():
    try:
        return json.loads(OVERRIDES.read_text())
    except FileNotFoundError:
        return {}


def scan_movies(row, tmdb, overrides, probes, jobs):
    items = []
    for entry in row_entries(row):
        files = video_files(entry)
        if not files:
            continue
        video = max(files, key=lambda f: f.stat().st_size)
        parsed = guess(entry.name if entry.is_dir() else entry.stem)
        override = overrides.get(f"{row['name']}/{entry.name}", {})
        title = override.get("title") or parsed["title"] or entry.stem
        year = override.get("year") or parsed["year"]

        details = None
        if row["tmdb"]:
            match = {"id": override["tmdb_id"]} if override.get("tmdb_id") else tmdb.search("movie", title, year)
            details = tmdb.get(f"/movie/{match['id']}") if match else None

        info = probe(video, probes)
        item = {
            "id": f"movie:{row['name']}/{entry.name}",
            "kind": "movie",
            "category": row["name"],
            "title": (details or {}).get("title") or title,
            "year": int(((details or {}).get("release_date") or "")[:4] or 0) or year,
            "overview": (details or {}).get("overview") or "",
            "tagline": (details or {}).get("tagline") or "",
            "rating": round((details or {}).get("vote_average") or 0, 1) or None,
            "genres": [g["name"] for g in (details or {}).get("genres", [])],
            "tmdb_id": (details or {}).get("id"),
            "path": str(video),
            "watch_later": watch_later_name(video),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "audio": info.get("audio", []),
            "subs": info.get("subs", []),
        }
        jobs.append((item["id"], POSTER_SIZE, (details or {}).get("poster_path"), video, 0.15))
        backdrop = tmdb.backdrop("movie", details["id"], details) if details else None
        jobs.append((item["id"] + "#card", CARD_SIZE, backdrop, video, 0.15))
        items.append(item)
    return items


def scan_series(row, tmdb, overrides, probes, jobs):
    category = row["name"]
    shows = {}
    for entry in row_entries(row):
        files = video_files(entry)
        if not files:
            continue
        override = overrides.get(f"{category}/{entry.name}", {})
        parsed = guess(entry.name if entry.is_dir() else entry.stem)
        title = override.get("title") or parsed["title"] or entry.name
        show = shows.setdefault(normalize(title), {"title": title, "year": override.get("year"), "tmdb_id": override.get("tmdb_id"), "files": [], "folders": []})
        show["folders"].append(entry.name)
        for f in files:
            g = guess(f.name)
            episode = g["episode"]
            if episode is None:
                continue  # extras, NCOP/NCED, samples
            season = g["season"] or season_from_parents(f, entry) or parsed["season"] or 1
            show["files"].append((season, episode, f, g["episode_title"]))

    items = []
    for key, show in sorted(shows.items()):
        details = None
        if row["tmdb"]:
            match = {"id": show["tmdb_id"]} if show["tmdb_id"] else tmdb.search("tv", show["title"], show["year"])
            details = tmdb.get(f"/tv/{match['id']}") if match else None

        seasons = {}
        if details:
            for season_no in sorted({s for s, _, _, _ in show["files"]}):
                data = tmdb.get(f"/tv/{details['id']}/season/{season_no}")
                seasons[season_no] = {e["episode_number"]: e for e in (data or {}).get("episodes", [])}

        series_id = f"series:{category}/{key}"
        episodes = []
        for season, episode, f, file_title in sorted(show["files"], key=lambda x: (x[0], x[1], str(x[2]))):
            meta = seasons.get(season, {}).get(episode, {})
            info = probe(f, probes)
            ep = {
                "id": f"{series_id}/s{season:02d}e{episode:02d}/{f.name}",
                "season": season,
                "episode": episode,
                "title": meta.get("name") or file_title or f"Episode {episode}",
                "overview": meta.get("overview") or "",
                "air_date": meta.get("air_date"),
                "path": str(f),
                "watch_later": watch_later_name(f),
                "duration": info.get("duration"),
                "width": info.get("width"),
            "height": info.get("height"),
                "audio": info.get("audio", []),
                "subs": info.get("subs", []),
            }
            jobs.append((ep["id"], STILL_SIZE, meta.get("still_path"), f, 0.3))
            episodes.append(ep)

        first_video = episodes[0]["path"] if episodes else None
        item = {
            "id": series_id,
            "kind": "series",
            "category": category,
            "title": (details or {}).get("name") or show["title"],
            "year": int(((details or {}).get("first_air_date") or "")[:4] or 0) or show["year"],
            "overview": (details or {}).get("overview") or "",
            "rating": round((details or {}).get("vote_average") or 0, 1) or None,
            "genres": [g["name"] for g in (details or {}).get("genres", [])],
            "tmdb_id": (details or {}).get("id"),
            "folders": show["folders"],
            "episodes": episodes,
        }
        jobs.append((series_id, POSTER_SIZE, (details or {}).get("poster_path"), first_video, 0.3))
        backdrop = tmdb.backdrop("tv", details["id"], details) if details else None
        jobs.append((series_id + "#card", CARD_SIZE, backdrop, first_video, 0.3))
        items.append(item)
    return items


def main():
    for d in (TMDB_CACHE, IMAGES, THUMBS):
        d.mkdir(parents=True, exist_ok=True)
    # mpv and the torrent sorter can start scans at the same time; never overlap them.
    lock = open(CACHE / "scan.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    tmdb = Tmdb()
    overrides = load_overrides()
    probes = load_probe_cache()
    jobs = []

    rows = load_rows()
    log(f"Scanning {', '.join(r['name'] for r in rows)} ({'online' if tmdb.online else 'offline: no TMDB token'})")
    items = []
    for row in rows:
        scan = scan_movies if row["type"] == "movies" else scan_series
        items += scan(row, tmdb, overrides, probes, jobs)
    PROBE_CACHE.write_text(json.dumps(probes))
    save_guess_cache()

    log(f"Making {len(jobs)} thumbnails")
    thumbs, images, sources = {}, {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for key, path, source, image in pool.map(make_thumb, jobs):
            thumbs[key] = path
            images[key] = image
            sources[source] = sources.get(source, 0) + 1
    # thumb, card: mpv's BGRA; poster, backdrop, still: the JPEGs behind them
    for item in items:
        item["thumb"] = thumbs.get(item["id"])
        item["card"] = thumbs.get(item["id"] + "#card")
        item["poster"] = images.get(item["id"])
        item["backdrop"] = images.get(item["id"] + "#card")
        for ep in item.get("episodes", []):
            ep["thumb"] = thumbs.get(ep["id"])
            ep["still"] = images.get(ep["id"])

    library = {
        "rows": [row["name"] for row in rows],
        "poster_size": POSTER_SIZE,
        "card_size": CARD_SIZE,
        "still_size": STILL_SIZE,
        "items": items,
    }
    # Only rewrite when something changed; mpv reloads the library when its mtime changes.
    text = json.dumps(library, indent=1, ensure_ascii=False)
    try:
        unchanged = LIBRARY.read_text() == text
    except OSError:
        unchanged = False
    if not unchanged:
        tmp = LIBRARY.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(LIBRARY)

    episodes = sum(len(i.get("episodes", [])) for i in items)
    tmdb_rows = {row["name"] for row in rows if row["tmdb"]}
    unmatched = [i["title"] for i in items if i["category"] in tmdb_rows and not i.get("tmdb_id")]
    log(f"Done: {len(items)} titles, {episodes} episodes; thumbnails {sources}; library {'unchanged' if unchanged else 'updated'}")
    if unmatched:
        log("Not found on TMDB: " + ", ".join(unmatched))


if __name__ == "__main__":
    main()
