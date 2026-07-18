# MediaDeck

A small self-hosted front end for `yt-dlp`.

## Run it

```bash
pip install -r requirements.txt
# yt-dlp also needs ffmpeg on PATH for audio extraction / muxing
python app.py
```

Then open `http://localhost:8080`.

## Interface

- **Single** — one title/URL at a time.
- **List** — a textarea, one entry per line (up to 100). Each line becomes
  its own job with its own status; when at least one finishes you get a
  "Download all as .zip" button that bundles everything done so far.
- **Playlist** — paste a public Spotify playlist link. MediaDeck reads the
  track and artist names via Spotify's API, shows a confirmation popup with
  the playlist cover, name, and full track list (deselect anything you
  don't want), then downloads each track as MP3 via the same YouTube
  search + yt-dlp path the List mode uses. A progress bar tracks how many
  of the playlist's songs have finished, and the bell icon opts into a
  browser notification when the run completes — on Android/Chrome this
  surfaces in the system notification shade.

### Spotify setup

Playlist import needs a free Spotify API app (read-only, client-credentials —
no user login involved, and only public playlists can be read):

1. Create an app at https://developer.spotify.com/dashboard
2. Copy its Client ID and Client Secret
3. Set them as environment variables before starting the server:

   ```bash
   export SPOTIFY_CLIENT_ID=your_client_id
   export SPOTIFY_CLIENT_SECRET=your_client_secret
   python app.py
   ```

Without these set, the Single and List tabs work as normal — only the
Playlist tab will report that Spotify import isn't configured.

## What changed from the original version

- **Non-blocking downloads.** The original ran `yt-dlp` synchronously inside
  the request, so anything slow tripped Flask/proxy timeouts. Downloads now
  run in background threads; the browser polls for status instead.
- **Batch queue.** `/api/batch` accepts a list of queries, launches one
  task per line, and a semaphore (`MEDIADECK_MAX_CONCURRENT`, default 3)
  caps how many `yt-dlp` processes run at once so a long list doesn't
  overwhelm the host.
- **Spotify playlist import.** `/api/spotify/resolve` reads a public
  playlist's metadata (name, cover, track/artist titles) via Spotify's
  Client Credentials API — read-only, no user auth, no audio ever touches
  Spotify's servers. The resolved track list feeds straight into the same
  batch engine as List mode.
- **Per-task directories.** Each job gets its own folder keyed by a UUID, so
  two similarly-named requests can never collide or return the wrong file.
- **Whitelisted quality options.** The client sends a preset key (`720p`,
  `1080p`, …), not a raw yt-dlp format string, so there's no way to inject
  extra format selectors through that field.
- **Real error surfacing.** yt-dlp failures, timeouts, and "no file
  produced" cases are caught and reported per-row with a specific message.
- **Automatic cleanup.** A background reaper deletes task folders older
  than `MEDIADECK_RETENTION_SECONDS` (default 15 minutes).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `MEDIADECK_DOWNLOAD_DIR` | `/tmp/mediadeck` | Where task files live |
| `MEDIADECK_RETENTION_SECONDS` | `900` | How long files stick around |
| `MEDIADECK_TIMEOUT_SECONDS` | `180` | Per-download timeout |
| `MEDIADECK_MAX_CONCURRENT` | `3` | Parallel `yt-dlp` processes across all jobs |
| `SPOTIFY_CLIENT_ID` | — | Required for the Playlist tab |
| `SPOTIFY_CLIENT_SECRET` | — | Required for the Playlist tab |
| `PORT` | `8080` | Server port |

## Note

This is a tool for downloading content you have the rights to (your own
uploads, Creative Commons/public-domain media, or anything else you're
authorized to download). Respect the terms of service of whatever site
you point it at and applicable copyright law.
