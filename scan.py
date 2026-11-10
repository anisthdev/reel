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
mpv's overlay-add, and the JPEGs they came from (poster, backdrop, still), plus
each title's transparent logo. Posters, details and episode stills come from TMDB when
online (token in ~/.config/reel/tmdb_token or $TMDB_TOKEN); anything
missing falls back to a frame grabbed from the video. Ratings: IMDb, Rotten
Tomatoes and Metacritic from OMDb (key in ~/.config/reel/omdb_key or
$OMDB_KEY), MyAnimeList's from Jikan for anime, TMDB's own.
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
# Logos keep their shape: scaled to fit this box, never up.
LOGO_BOX = (500, 200)
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p"
TMDB_TTL = 30 * 24 * 3600
# A show still on the air: its details (status, next episode) kept a day
AIRING_TTL = 24 * 3600
OMDB_API = "https://www.omdbapi.com/"
OMDB_CACHE = CACHE / "omdb"
JIKAN_API = "https://api.jikan.moe/v4"
JIKAN_CACHE = CACHE / "jikan"
# Ratings move slowly; a week is fresh enough
RATINGS_TTL = 7 * 24 * 3600
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

    def get(self, path, ttl=TMDB_TTL, **params):
        url = f"{TMDB_API}{path}?" + urllib.parse.urlencode(sorted(params.items()))
        cache_file = TMDB_CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".json")
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
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

    def logo(self, kind, tmdb_id):
        """The title's logo (transparent PNG): English, else language-free.
        Same images request as backdrop(), so it's already cached."""
        images = self.get(f"/{kind}/{tmdb_id}/images", include_image_language="en,null") or {}
        logos = [l for l in images.get("logos") or [] if (l.get("file_path") or "").endswith(".png")]
        for language in ("en", None):
            choices = [l for l in logos if l.get("iso_639_1") == language]
            if choices:
                best = max(choices, key=lambda l: (l.get("vote_average") or 0, l.get("width") or 0))
                return best["file_path"]
        return None

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


# --- ratings ----------------------------------------------------------------


def cached_json(cache_dir, url, ttl, fetch):
    """url's JSON from cache_dir if younger than ttl, else fetch() (None: keep
    whatever is cached, however old)."""
    cache_file = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
        return json.loads(cache_file.read_text())
    data = fetch()
    if data is None:
        return json.loads(cache_file.read_text()) if cache_file.exists() else None
    cache_file.write_text(json.dumps(data))
    return data


def number(text):
    """The number in OMDb's text: 8.6, 1,234,567, 96%, 87/100; N/A -> None."""
    m = re.match(r"[\d,.]+", (text or "").strip())
    if not m:
        return None
    value = float(m.group().replace(",", ""))
    return int(value) if value.is_integer() and "." not in m.group() else value


class Omdb:
    """IMDb's rating and votes, Rotten Tomatoes and Metacritic, by IMDb id."""

    def __init__(self):
        key = os.environ.get("OMDB_KEY")
        key_file = CONFIG / "omdb_key"
        if not key and key_file.exists():
            key = key_file.read_text().strip()
        self.key = key
        self.online = bool(key)
        OMDB_CACHE.mkdir(parents=True, exist_ok=True)

    def fetch(self, imdb_id):
        url = f"{OMDB_API}?" + urllib.parse.urlencode({"i": imdb_id, "apikey": self.key})
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            # 401: a bad key, or the day's 1000 calls used up
            log(f"OMDb refused ({exc.code}: {exc.read().decode(errors='replace')[:100]}); ratings from cache")
            self.online = False
            return None
        except OSError as exc:
            log(f"OMDb unreachable ({exc}); ratings from cache")
            self.online = False
            return None
        if data.get("Response") == "False" and "not found" not in (data.get("Error") or "").lower():
            log(f"OMDb: {data.get('Error')}; ratings from cache")
            self.online = False
            return None
        return data

    def ratings(self, imdb_id):
        if not imdb_id:
            return {}
        url = f"{OMDB_API}?i={imdb_id}"  # the cache key, without the key
        data = cached_json(OMDB_CACHE, url, RATINGS_TTL, lambda: self.fetch(imdb_id) if self.online else None) or {}
        result = {"imdb": number(data.get("imdbRating")), "imdb_votes": number(data.get("imdbVotes"))}
        for r in data.get("Ratings") or []:
            if r.get("Source") == "Rotten Tomatoes":
                result["rt"] = number(r.get("Value"))
            elif r.get("Source") == "Metacritic":
                result["metacritic"] = number(r.get("Value"))
        return {k: v for k, v in result.items() if v is not None}


class Jikan:
    """MyAnimeList's score, through Jikan (no key; 3 calls a second)."""

    def __init__(self, online):
        self.online = online
        self.last = 0.0
        JIKAN_CACHE.mkdir(parents=True, exist_ok=True)

    def fetch(self, url):
        if not self.online:
            return None
        for attempt in range(3):
            time.sleep(max(0, self.last + 0.4 - time.time()))
            self.last = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "reel-scan"}), timeout=8) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if exc.code == 404:
                    return {}
                log(f"Jikan refused ({exc.code}); MAL scores from cache")
                break
            except OSError as exc:
                log(f"Jikan unreachable ({exc}); MAL scores from cache")
                break
        self.online = False
        return None

    def get(self, path, **params):
        url = f"{JIKAN_API}{path}?" + urllib.parse.urlencode(sorted(params.items()))
        return cached_json(JIKAN_CACHE, url, RATINGS_TTL, lambda: self.fetch(url))

    def find(self, titles, year, kind):
        """The MAL entry for a TMDB title: a name of it, its year, its kind
        (a series' first season is MAL's TV entry that aired that year)."""
        types = {"Movie"} if kind == "movie" else {"TV", "ONA", "OVA"}
        wanted = {normalize(t) for t in titles if t}
        best = None
        for title in [t for t in titles if t][:2]:
            data = self.get("/anime", q=title, limit=10)
            for a in (data or {}).get("data") or []:
                names = {normalize(a.get(k)) for k in ("title", "title_english", "title_japanese")}
                names |= {normalize(t.get("title")) for t in a.get("titles") or []}
                aired = ((a.get("aired") or {}).get("prop") or {}).get("from") or {}
                score = (bool(names & wanted) * 4 + (aired.get("year") == year) * 2 + (a.get("type") in types) * 2
                         + bool(year and aired.get("year") and abs(aired["year"] - year) <= 1))
                if score >= 6 and (best is None or (score, a.get("members") or 0) > best[0]):
                    best = ((score, a.get("members") or 0), a)
            if best and best[0][0] >= 8:
                break
        return best[1] if best else None

    def ratings(self, mal_id=None, titles=(), year=None, kind="tv"):
        if mal_id:
            a = ((self.get(f"/anime/{mal_id}") or {}).get("data")) or {}
        else:
            a = self.find(titles, year, kind) or {}
        result = {"mal": a.get("score"), "mal_votes": a.get("scored_by"), "mal_id": a.get("mal_id")}
        return {k: v for k, v in result.items() if v is not None}


def is_anime(details):
    return (details or {}).get("original_language") == "ja" and any(g.get("id") == 16 for g in (details or {}).get("genres") or [])


def ratings(details, imdb_id, omdb, jikan, override, kind):
    """Every score known for a title: imdb (+ imdb_votes), rt (%), metacritic,
    tmdb (+ tmdb_votes), mal (+ mal_votes, mal_id) for anime."""
    result = {}
    if details and details.get("vote_count"):
        result["tmdb"] = round(details.get("vote_average") or 0, 1)
        result["tmdb_votes"] = details["vote_count"]
    result |= omdb.ratings(imdb_id)
    if is_anime(details) or override.get("mal_id"):
        name, original = ("title", "original_title") if kind == "movie" else ("name", "original_name")
        date = (details or {}).get("release_date" if kind == "movie" else "first_air_date") or ""
        result |= jikan.ratings(override.get("mal_id"), ((details or {}).get(name), (details or {}).get(original)), int(date[:4] or 0) or None, kind)
    return result


ENDED = {"Ended", "Canceled"}


def air_status(details):
    """A show's state: status (returning, ended, canceled, upcoming), the
    next episode to air and the last that did. Whether it's airing right
    now is the reader's call, by those dates."""
    if not details:
        return {}
    status = {"Returning Series": "returning", "Ended": "ended", "Canceled": "canceled"}.get(details.get("status"), "upcoming")
    nxt, last = details.get("next_episode_to_air") or {}, details.get("last_episode_to_air") or {}
    result = {"status": status, "network": ((details.get("networks") or [{}])[0]).get("name")}
    if nxt.get("air_date"):
        result["next_air"] = {"date": nxt["air_date"], "season": nxt.get("season_number"), "episode": nxt.get("episode_number")}
    if last.get("air_date"):
        result["last_air"] = {"date": last["air_date"], "season": last.get("season_number"), "episode": last.get("episode_number")}
    return {k: v for k, v in result.items() if v is not None}


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


def added(path):
    """When a file came into the library: moving it in (as sort.py does)
    changes its ctime, downloading it its mtime."""
    stat = path.stat()
    return int(max(stat.st_mtime, stat.st_ctime))


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


def make_logo(job):
    """job: (key, TMDB logo path). Returns (key, BGRA file, [w, h], PNG).

    Unlike thumbnails, logos keep their own aspect ratio and transparency,
    so the BGRA is premultiplied (as overlay-add wants) and its size varies.
    """
    key, image_path = job
    original = IMAGES / ("w500" + image_path.replace("/", "_"))
    if not download(f"{TMDB_IMAGE}/w500{image_path}", original):
        return key, None, None, None
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(original)],
        capture_output=True, text=True,
    )
    try:
        width, height = (int(v) for v in out.stdout.strip().split(",")[:2])
    except ValueError:
        return key, None, None, None
    scale = min(LOGO_BOX[0] / width, LOGO_BOX[1] / height, 1)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    dest = THUMBS / f"{hashlib.sha1(image_path.encode()).hexdigest()[:16]}_logo_{w}x{h}.bgra"
    if not dest.exists():
        tmp = dest.with_suffix(".tmp")
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(original),
             "-vf", f"scale={w}:{h}:flags=lanczos,format=rgba,premultiply=inplace=1,format=bgra",
             "-frames:v", "1", "-f", "rawvideo", str(tmp)],
            capture_output=True,
        )
        if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size != w * h * 4:
            tmp.unlink(missing_ok=True)
            return key, None, None, None
        tmp.replace(dest)
    return key, str(dest), [w, h], str(original)


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


def scan_movies(row, tmdb, omdb, jikan, overrides, probes, jobs, logo_jobs):
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
            "ratings": ratings(details, (details or {}).get("imdb_id"), omdb, jikan, override, "movie"),
            "genres": [g["name"] for g in (details or {}).get("genres", [])],
            "tmdb_id": (details or {}).get("id"),
            "imdb_id": (details or {}).get("imdb_id") or None,
            "path": str(video),
            "watch_later": watch_later_name(video),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "audio": info.get("audio", []),
            "subs": info.get("subs", []),
            "added": added(video),
        }
        jobs.append((item["id"], POSTER_SIZE, (details or {}).get("poster_path"), video, 0.15))
        backdrop = tmdb.backdrop("movie", details["id"], details) if details else None
        jobs.append((item["id"] + "#card", CARD_SIZE, backdrop, video, 0.15))
        logo = tmdb.logo("movie", details["id"]) if details else None
        if logo:
            logo_jobs.append((item["id"], logo))
        items.append(item)
    return items


def scan_series(row, tmdb, omdb, jikan, overrides, probes, jobs, logo_jobs):
    category = row["name"]
    shows = {}
    for entry in row_entries(row):
        files = video_files(entry)
        if not files:
            continue
        override = overrides.get(f"{category}/{entry.name}", {})
        parsed = guess(entry.name if entry.is_dir() else entry.stem)
        title = override.get("title") or parsed["title"] or entry.name
        show = shows.setdefault(normalize(title), {"title": title, "year": override.get("year"), "tmdb_id": override.get("tmdb_id"), "mal_id": override.get("mal_id"), "files": [], "folders": []})
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
            # Still on the air: its status and next episode a day old at most
            if details and details.get("status") not in ENDED:
                details = tmdb.get(f"/tv/{match['id']}", ttl=AIRING_TTL) or details
        imdb_id = ((tmdb.get(f"/tv/{details['id']}/external_ids") or {}).get("imdb_id") or None) if details else None

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
                "added": added(f),
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
            "ratings": ratings(details, imdb_id, omdb, jikan, {"mal_id": show["mal_id"]} if show["mal_id"] else {}, "tv"),
            **air_status(details),
            "genres": [g["name"] for g in (details or {}).get("genres", [])],
            "tmdb_id": (details or {}).get("id"),
            "imdb_id": imdb_id,
            "folders": show["folders"],
            "episodes": episodes,
            # When its latest episode came in
            "added": max((e["added"] for e in episodes), default=0),
        }
        jobs.append((series_id, POSTER_SIZE, (details or {}).get("poster_path"), first_video, 0.3))
        backdrop = tmdb.backdrop("tv", details["id"], details) if details else None
        jobs.append((series_id + "#card", CARD_SIZE, backdrop, first_video, 0.3))
        logo = tmdb.logo("tv", details["id"]) if details else None
        if logo:
            logo_jobs.append((series_id, logo))
        items.append(item)
    return items


def main():
    for d in (TMDB_CACHE, IMAGES, THUMBS):
        d.mkdir(parents=True, exist_ok=True)
    # mpv and the torrent sorter can start scans at the same time; never overlap them.
    lock = open(CACHE / "scan.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    tmdb = Tmdb()
    omdb = Omdb()
    jikan = Jikan(tmdb.online)
    overrides = load_overrides()
    probes = load_probe_cache()
    jobs, logo_jobs = [], []

    rows = load_rows()
    log(f"Scanning {', '.join(r['name'] for r in rows)} ({'online' if tmdb.online else 'offline: no TMDB token'}{'' if omdb.online else '; no OMDb key, so no IMDb, RT or Metacritic'})")
    items = []
    for row in rows:
        scan = scan_movies if row["type"] == "movies" else scan_series
        items += scan(row, tmdb, omdb, jikan, overrides, probes, jobs, logo_jobs)
    PROBE_CACHE.write_text(json.dumps(probes))
    save_guess_cache()

    log(f"Making {len(jobs)} thumbnails")
    thumbs, images, sources = {}, {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for key, path, source, image in pool.map(make_thumb, jobs):
            thumbs[key] = path
            images[key] = image
            sources[source] = sources.get(source, 0) + 1
        logos = {key: (path, size, image) for key, path, size, image in pool.map(make_logo, logo_jobs) if path}
    # thumb, card: mpv's BGRA; poster, backdrop, still: the JPEGs behind them
    for item in items:
        item["thumb"] = thumbs.get(item["id"])
        item["card"] = thumbs.get(item["id"] + "#card")
        item["poster"] = images.get(item["id"])
        item["backdrop"] = images.get(item["id"] + "#card")
        # logo: mpv's BGRA (premultiplied, logo_size big); logo_image: its PNG
        item["logo"], item["logo_size"], item["logo_image"] = logos.get(item["id"], (None, None, None))
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
    log(f"Done: {len(items)} titles, {episodes} episodes; thumbnails {sources}; logos {len(logos)}; library {'unchanged' if unchanged else 'updated'}")
    if unmatched:
        log("Not found on TMDB: " + ", ".join(unmatched))


if __name__ == "__main__":
    main()
