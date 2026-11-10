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

Each movie, episode and series records when it was `added` (a file's
mtime or ctime, whichever is later; a series' latest episode), for
quickbar's video menu to sort by.

Both are `uv run --script` scripts (guessit is their one dependency).
`makepkg -si` with the PKGBUILD installs them as `reel-scan` and
`reel-sort`.

## library.json

What other programs read (mpv's start screen, quickbar's video menu):
`$XDG_CACHE_HOME/reel/library.json`, rewritten only when something changed.

```
{"rows": ["Movies", "TV", …],             the rows, in order
 "poster_size": [w, h], "card_size": [w, h], "still_size": [w, h],
 "items": [ITEM, …]}

ITEM     id, kind ("movie" | "series"), category (its row), title, year,
         overview, tagline, rating (TMDB's), ratings: {imdb, imdb_votes,
         rt (%), metacritic, tmdb, tmdb_votes, mal, mal_votes, mal_id},
         only the ones known, genres, tmdb_id, imdb_id, added (epoch),
         thumb, card (BGRA files for mpv), poster, backdrop (JPEGs),
         logo (premultiplied BGRA for mpv), logo_size ([w, h], fits
         500x200), logo_image (the transparent PNG);
         a movie: path, watch_later (mpv's resume file name), duration,
         width, height, audio, subs (languages);
         a series: folders, episodes: [EPISODE, …], status ("returning" |
         "ended" | "canceled" | "upcoming"), network, next_air, last_air
         ({date, season, episode}; whether it's airing now is the
         reader's call, by those dates)
EPISODE  id, season, episode, title, overview, air_date, path,
         watch_later, duration, width, height, audio, subs, added,
         thumb, still
```

Fields may be missing or null when nothing is known (offline, say).

## Configuration

`~/.config/reel/` (in the dotfiles):

- `library.toml`: the rows, their folders, and where each kind of torrent
  goes (see the file).
- `overrides.json`: the title, year or TMDB id for files whose names mislead
  (keyed by row name and path, e.g. `"Movies/500_days_of_summer.mkv"`).
- `tmdb_token`: a TMDB API read token (or `$TMDB_TOKEN`); kept out of git.
- `omdb_key`: an OMDb API key (or `$OMDB_KEY`; free at omdbapi.com) for
  IMDb, Rotten Tomatoes and Metacritic ratings; kept out of git. Anime get
  MyAnimeList's score through Jikan, no key; `mal_id` in overrides.json
  fixes a wrong match.
  Without it everything works offline, from file names and video frames, but
  `sort.py` leaves torrents where they are.

Caches (TMDB responses, guesses, probes, images, `sort.log`) are in
`~/.cache/reel/`.

## Use

```sh
reel-scan                       # rescan
reel-sort --id 12 --dry-run     # what would happen to torrent 12
reel-sort --classify "NAME"     # classify a name only
```

Transmission runs it as `script-torrent-done-filename: /usr/bin/reel-sort`
in `~/.config/transmission-daemon/settings.json`.
