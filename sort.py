#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["guessit"]
# ///
"""Move finished torrents into the library, via Transmission so seeding continues.

Runs as Transmission's torrent-done script (TR_TORRENT_* in the environment):
classifies the torrent with guessit + TMDB, asks Transmission to move it to the
row in ~/.config/reel/library.toml whose `sort` matches (movies, series or anime), then
rescans the library. Anything it isn't sure about stays where it is, with a
notification saying why.

Manual use:
  sort.py --id 12 --dry-run      what would happen to torrent 12
  sort.py --classify "NAME"      classify a torrent name only
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ next to scan.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan  # noqa: E402  (shares guessit cache, TMDB client and DNS workaround)

LOG = scan.CACHE / "sort.log"
MOVE_TIMEOUT = 600
ANIMATION_GENRE = 16


def log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, file=sys.stderr, flush=True)


def notify(summary, body, error=False):
    subprocess.run(
        ["notify-send", "--app-name=Torrents", f"--icon={'dialog-warning' if error else 'video-x-generic'}", summary, body],
        capture_output=True,
    )


def notify_added(summary, body, video, folder):
    """The done notification, with Play (the video in mpv) and Open folder (yazi).

    notify-send waits for the pick, so it runs on its own, outside Transmission's
    hook; the script carries on (and exits) meanwhile.
    """
    script = """
        case "$(notify-send --app-name=Torrents --icon=video-x-generic \
            --action=play=Play --action=open='Open folder' "$1" "$2")" in
        play) exec uwsm app -- mpv --player-operation-mode=pseudo-gui -- "$3" ;;
        open) exec uwsm app -- ghostty --title=yazi -e yazi "$4" ;;
        esac
    """
    subprocess.Popen(
        ["sh", "-c", script, "sh", summary, body, str(video), str(folder)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- classification ---------------------------------------------------------


def titles_match(query, *candidates):
    """Same normalized title, or the query is a leading part of it (KonoSuba)."""
    wanted = scan.normalize(query)
    if not wanted:
        return False
    for candidate in candidates:
        name = scan.normalize(candidate)
        if name == wanted or (len(wanted) >= 6 and name.startswith(wanted + " ")):
            return True
    return False


def confident(tmdb, kind, result, title, year):
    """Is this TMDB result really the title guessit found?"""
    name_key = "title" if kind == "movie" else "name"
    date_key = "release_date" if kind == "movie" else "first_air_date"
    if titles_match(title, result.get(name_key), result.get("original_" + name_key)):
        return True
    result_year = scan.first_int((result.get(date_key) or "")[:4])
    if kind == "movie" and year and result_year == year:
        return True
    # Romaji and other release names are listed as alternative titles.
    alternatives = tmdb.get(f"/{kind}/{result['id']}/alternative_titles") or {}
    names = [t.get("title") for t in alternatives.get("titles") or alternatives.get("results") or []]
    return titles_match(title, *names)


def is_episodic(name, files):
    g = scan.guess(name)
    if g["type"] == "episode" or g["season"] is not None or g["episode"] is not None:
        return True
    return sum(1 for f in files if scan.guess(f.name)["episode"] is not None) >= 2


def classify(tmdb, name, files):
    """Returns {"kind": "movies"|"series"|"anime", ...} or {"reason": ...}."""
    g = scan.guess(name)
    title, year = g["title"], g["year"]
    if not title:
        return {"reason": "couldn't read a title from the name"}
    if not tmdb.online:
        return {"reason": "TMDB isn't reachable"}

    if not is_episodic(name, files):
        result = tmdb.search("movie", title, year)
        if not result or not confident(tmdb, "movie", result, title, year):
            return {"reason": f"no confident TMDB movie match for “{title}”" + (f" ({year})" if year else "")}
        return {"kind": "movies", "title": result["title"], "year": (result.get("release_date") or "")[:4]}

    result = tmdb.search("tv", title, year)
    if not result or not confident(tmdb, "tv", result, title, year):
        return {"reason": f"no confident TMDB series match for “{title}”"}
    details = tmdb.get(f"/tv/{result['id']}")
    if not details:
        # Without genres and origin, anime vs TV would only be a guess.
        return {"reason": f"couldn't fetch TMDB details for “{result['name']}”"}
    genres = {genre["id"] for genre in details.get("genres", [])}
    japanese = "JP" in (details.get("origin_country") or []) or details.get("original_language") == "ja"
    kind = "anime" if ANIMATION_GENRE in genres and japanese else "series"
    return {"kind": kind, "title": details.get("name") or result["name"], "guess_title": title}


# --- transmission -----------------------------------------------------------


def transmission(*args):
    return subprocess.run(["transmission-remote", *args], capture_output=True, text=True)


def torrent_info(torrent_id):
    out = transmission("-t", str(torrent_id), "-i").stdout
    info = {}
    for key in ("Name", "Location"):
        match = re.search(rf"^\s*{key}: (.*)$", out, re.M)
        info[key.lower()] = match.group(1).strip() if match else None
    return info


def wait_for_move(torrent_id, dest):
    deadline = time.time() + MOVE_TIMEOUT
    while time.time() < deadline:
        location = torrent_info(torrent_id).get("location") or ""
        if Path(location.rstrip("/")) == dest:
            return True
        time.sleep(2)
    return False


# --- destination ------------------------------------------------------------


def destinations():
    return {row["sort"]: row["paths"][0] for row in scan.load_rows() if row.get("sort") and row["paths"]}


def safe_name(name):
    return re.sub(r'[/\\:*?"<>|]+', " ", name).strip(" .")


def destination(decision, root, dest_root):
    """Folders go straight into the row; a lone episode file gets a show folder."""
    if decision["kind"] == "movies" or root.is_dir():
        return dest_root
    for entry in sorted(dest_root.iterdir()) if dest_root.exists() else []:
        if entry.is_dir() and titles_match(scan.guess(entry.name)["title"] or "", decision["title"], decision.get("guess_title")):
            return entry
    return dest_root / safe_name(decision["title"])


# --- main -------------------------------------------------------------------


def sort_torrent(torrent_id, name, directory, dry_run=False):
    root = Path(directory) / name
    if not root.exists():
        log(f"#{torrent_id} {name}: files not found at {root}, leaving it")
        if not dry_run:
            notify("Torrent files missing", f"{name}\nNot found at {root}.", error=True)
        return
    files = scan.video_files(root)
    files = [f for f in files if "sample" not in f.name.lower()]
    if not files:
        log(f"#{torrent_id} {name}: no video files, leaving it")
        if not dry_run:
            notify("Left in Downloads", f"{name}\nNo video files in this torrent.", error=True)
        return

    tmdb = scan.Tmdb()
    decision = classify(tmdb, name, files)
    scan.save_guess_cache()
    if "reason" in decision:
        log(f"#{torrent_id} {name}: {decision['reason']}, leaving it")
        if not dry_run:
            notify("Left in Downloads", f"{name}\n{decision['reason'].capitalize()}.", error=True)
        return

    dest_root = destinations().get(decision["kind"])
    if not dest_root:
        log(f"#{torrent_id} {name}: no library.toml row with sort = \"{decision['kind']}\", leaving it")
        return
    dest = destination(decision, root, dest_root)
    label = f"{decision['title']}" + (f" ({decision['year']})" if decision.get("year") else "")
    if Path(directory).resolve() == dest.resolve():
        log(f"#{torrent_id} {name}: already in {dest}")
        return
    if (dest / name).exists():
        log(f"#{torrent_id} {name}: {dest / name} already exists, leaving it")
        if not dry_run:
            notify("Left in Downloads", f"{name}\nSomething with this name is already in {dest}.", error=True)
        return

    log(f"#{torrent_id} {name}: {decision['kind']} “{label}” → {dest}" + (" (dry run)" if dry_run else ""))
    if dry_run:
        return

    result = transmission("-t", str(torrent_id), "--move", str(dest))
    if result.returncode != 0 or not wait_for_move(torrent_id, dest):
        log(f"#{torrent_id} {name}: move failed: {result.stderr.strip() or 'timed out'}")
        notify("Couldn't move torrent", f"{label}\n{result.stderr.strip() or 'Transmission did not finish moving it.'}", error=True)
        return

    # What to play once it's there: a movie's main file, a series' first episode
    moved = [dest / f.relative_to(Path(directory)) for f in files]
    video = max(moved, key=lambda f: f.stat().st_size if f.exists() else 0) if decision["kind"] == "movies" else moved[0]
    notify_added(f"Added to {dest_root.name}", label, video, dest / name if root.is_dir() else dest)
    scan.main()  # refresh the library so mpv's start screen has it next time


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--id", type=int, help="torrent id (default: TR_TORRENT_ID)")
    parser.add_argument("--classify", metavar="NAME", help="only classify a torrent name")
    parser.add_argument("--dry-run", action="store_true", help="report the decision without moving anything")
    args = parser.parse_args()

    if args.classify:
        decision = classify(scan.Tmdb(), args.classify, [])
        scan.save_guess_cache()
        print(decision)
        return

    torrent_id = args.id or os.environ.get("TR_TORRENT_ID")
    if not torrent_id:
        parser.error("no torrent: pass --id or run as Transmission's torrent-done script")

    hook = "TR_TORRENT_ID" in os.environ and not args.id
    if hook:
        # Transmission discards the script's output; keep a log instead.
        scan.CACHE.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(LOG, "a", buffering=1)
        name, directory = os.environ["TR_TORRENT_NAME"], os.environ["TR_TORRENT_DIR"]
    else:
        info = torrent_info(torrent_id)
        name, directory = info["name"], info["location"]
        if not name:
            parser.error(f"no torrent with id {torrent_id}")

    try:
        sort_torrent(torrent_id, name, directory, dry_run=args.dry_run)
    except Exception as exc:  # never leave Transmission's hook silently broken
        log(f"#{torrent_id} {name}: error: {exc!r}")
        if not args.dry_run:
            notify("Torrent sorting failed", f"{name}\n{exc}", error=True)
        raise


if __name__ == "__main__":
    main()
