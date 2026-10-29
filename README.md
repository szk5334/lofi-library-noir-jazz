# NIGHTDECK library

Music for the NIGHTDECK radio player. Everything is Creative Commons or public domain; see `CREDITS.md`.

- `tracks.json`: the curated list. Edit this, and the **Build library** action downloads, converts (112 kbps) and credits everything automatically.
- Remove a track from `tracks.json` and the next run deletes its audio.
- `manifest.json`: what the player reads (only tracks that downloaded).
- `fetch-report.json`: what worked and what didn't on the last run.
