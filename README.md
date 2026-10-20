# reel

The video library behind mpv's start screen: what's in ~/Videos, with
posters and details from TMDB, and finished torrents sorted into it.

- `scan.py` scans the rows' folders into `~/.cache/reel/library.json`
  (titles, episodes, TMDB details) plus thumbnails as raw BGRA for mpv's
  `overlay-add`. mpv's start screen (uosc's Library element) reads it and
  runs the scan.
- `sort.py` is Transmission's torrent-done script: it classifies a finished
  torrent (guessit + TMDB), has Transmission move it to the matching row's
  folder, so seeding carries on, then rescans. Its notification offers Play
  and Open folder; anything it isn't sure of stays put, with a notification
  saying why.

Both are `uv run --script` scripts (guessit is their one dependency).

## Configuration

`~/.config/reel/` (in the dotfiles):

- `library.toml`: the rows, their folders, and where each kind of torrent
  goes (see the file).
- `overrides.json`: the title, year or TMDB id for files whose names mislead
  (keyed by row name and path, e.g. `"Movies/500_days_of_summer.mkv"`).
- `tmdb_token`: a TMDB API read token (or `$TMDB_TOKEN`); kept out of git.
  Without it everything works offline, from file names and video frames, but
  `sort.py` leaves torrents where they are.

Caches (TMDB responses, guesses, probes, images, `sort.log`) are in
`~/.cache/reel/`.

## Use

```sh
./scan.py                       # rescan
./sort.py --id 12 --dry-run     # what would happen to torrent 12
./sort.py --classify "NAME"     # classify a name only
```

Transmission runs `sort.py` from `script-torrent-done-filename` in
`~/.config/transmission-daemon/settings.json`.
