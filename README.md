# Shiftarr — per-segment subtitle sync for re-edited broadcasts

Automatically re-times a video's **embedded** English subtitle to its audio and writes an
external `<episode>.en.srt` sidecar next to the file. Built for broadcast shows ripped as
WEB-DL where ad breaks have been cut out, so the broadcast subtitle **drifts** progressively
later through the episode (e.g. Taskmaster: 0s at the start → ~9s late by the end).

Plain global sync (ffsubsync/alass "one offset") and audio-only tools fail on this content
because the drift is piecewise and the studio applause/laughter defeats voice-activity
matching. This tool fixes both.

Shiftarr slots into an *arr media stack: Sonarr calls it on import (webhook), it reads the
embedded sub, per-segment re-times it, writes the sidecar, and optionally tells Plex to select
that track. It has **no runtime dependencies beyond `ffmpeg` + `ffsubsync`** — everything else
is Python stdlib, configured entirely through environment variables (see below).

---

## How it works

```
Sonarr imports an episode
        │  (On Import / On Upgrade  →  Connect → Webhook)
        ▼
POST http://shiftarr:8000/          server.py  (tiny HTTP listener, no docker socket needed)
        │  episodeFile.path = /tv/…/Episode.mkv
        ▼
shiftarr.py <video>
   1. allowlist check (allowlist.txt: process only matching paths)
   2. ffprobe → pick the embedded English subrip stream + the audio stream
   3. ffmpeg → extract that embedded sub to <base>.en.srt
   4. split the runtime into fixed windows (default 10 min)
   5. for each window, in order:
        • pre-shift the window's cues by the running drift `prev`
        • extract that window's audio to 16 kHz mono wav
        • ffsubsync finds only the *residual* shift (search bound ±STEP_CAP)
        • new drift = prev + residual   →   becomes `prev` for the next window
   6. apply the per-window drift (+ optional global LEAD) to every cue
   7. overwrite <base>.en.srt
```

### Why "pre-shift the residual" instead of an absolute search

The drift accumulates per ad break and is **unbounded** (4 breaks × ~4.5 s ≈ 18 s; more
for ad-heavy shows). But a large absolute ffsubsync search invites **spurious** matches — a
window of theme music can "align" 30 s off. By pre-shifting each window by the running drift
and letting ffsubsync search only a small residual (`STEP_CAP`, default ±12 s):

* the **total** drift can grow without limit (it accumulates window to window), and
* no single window can jump far from the running value → spurious far matches are impossible,
  even on the first window.

This was the key fix after simpler approaches failed (see "History" below).

### No embedded English subtitle

`sub_index()` only uses an embedded subrip stream tagged `SHIFTARR_LANG` **or untagged/`und`** — it never
extracts a stream tagged as a *different* language (e.g. `swe`) and mislabels it `.en.srt`. When a video has
no usable embedded English sub, shiftarr instead **syncs an existing external English sidecar in place** —
`<base>.en.srt` if present, else Bazarr's SDH `<base>.en.hi.srt` — backing up the un-synced original to
`<name>.orig` first. So episodes whose broadcast rip carries only a foreign embedded sub still get a
time-aligned English track (from Bazarr), and Plex's per-item selection points at it. If there's no English
source at all, shiftarr logs `SKIP` and leaves the file untouched.

### Break-accurate refinement (`SHIFTARR_REFINE=1`)

The fixed 10-min grid puts each drift step on a round boundary, but the real ad-break end is a few minutes
off — so ~1–2 min around each break carries the neighbouring window's offset and is a few seconds out. When
`SHIFTARR_REFINE=1`, each detected step is moved to the **true break end**, located from three cheap,
mutually-reinforcing signals (audio + text only, no video decode — the old black-frame approach):

1. **Cue-gaps** — an ad break leaves a stretch with no dialogue, so the break end is the *start of a cue that
   follows a ≥6 s gap*. These are far fewer/cleaner than audio silences, so they replace a fragile continuous
   search with a handful of real candidates.
2. **Return-words** — the returning line is a "welcome back / part three / …" (`SHIFTARR_RETURN_WORDS`, plus any
   per-series words mined by `--learn-words`). Among the gap candidates, the one with the most return-words wins.
3. **VAD** — ffsubsync's speech model confirms which candidate is the point where the audio switches from
   fitting the pre-break offset to the post-break one.

A **per-season cut map** (`SHIFTARR_CUTS_FILE`) records what's found and seeds later episodes (breaks land at
similar times within a season — but *not* across seasons, since episode length changes, so it's keyed per
season). Any signal can be missing and the others carry it; if none agree, the fixed boundary is kept (never
worse than off). ~+10 s per break, scaling with break count. Mine per-series return-words with:

```bash
docker exec shiftarr python /app/shiftarr.py --learn-words "/tv/Taskmaster"/*/*.mkv
```

---

## Files

| File | Purpose |
|------|---------|
| `shiftarr.py` | The sync logic. `shiftarr.py <video.mkv> [...]` — extracts + syncs, writes `<base>.en.srt`. |
| `plex.py`        | Optional: after the sidecar is written, tells Plex to *select* it for that episode (stdlib-only, no-op unless `PLEX_TOKEN` is set). See "Auto-select in Plex". |
| `server.py`      | Webhook listener on `:8000`. Sonarr POSTs here on import; runs the sync in a background thread. |
| `Dockerfile`     | `python:3.12-slim` + `ffmpeg` + `pip install ffsubsync` (installs from wheels — no compiler). |
| `allowlist.txt`  | One substring per line; a video is processed only if its path contains one. Mounted, so edits apply immediately (no rebuild). |
| `docker-compose.yml` | Example service definition — copy the `shiftarr` service into your own compose stack and set the env vars for your setup. |

---

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `SHIFTARR_SEG_MIN`  | `10` | Window length in minutes. Smaller = finer near breaks, but too small (≤5) makes music-only intros mis-align. 10 is the sweet spot. |
| `SHIFTARR_STEP_CAP` | `12` | Max drift change allowed per window (residual search bound, seconds). |
| `SHIFTARR_LEAD`     | `0`  | Lead (s) **applied only to segments that are actually drift-corrected** — it rides along with the shift, so an accurate segment stays put. **Negative = earlier.** e.g. `-0.2`. |
| `SHIFTARR_SNAP`     | `0.3` | A segment whose drift is below this (s) is treated as **0** — no shift **and no lead**, so an already-accurate sub (or segment) is left exactly as-is. |
| `SHIFTARR_LANG`     | `eng` | Embedded subtitle stream language to prefer. |
| `SHIFTARR_TAG`      | `en` | Sidecar suffix → `<base>.<TAG>.srt`. |
| `SHIFTARR_ALLOWLIST`| `/app/allowlist.txt` | Path to the allowlist. |
| `SHIFTARR_LOG`      | `/config/shiftarr.log` | Log file (also visible via `docker logs shiftarr`). |
| `SHIFTARR_REFINE`   | `0` | Move each drift step from the fixed grid to the **true ad-break end** (see "Break-accurate refinement"). Off = plain fixed windows. |
| `SHIFTARR_RETURN_WORDS` | *(mined set)* | Return-from-break keywords used to pick the break cue-gap (`welcome,part,one…back`). Extended per-series by `--learn-words`. |
| `SHIFTARR_CUTS_FILE`| `/config/.shiftarr_cuts.json` | Learned per-season cut map + per-series `_words`. |
| `PLEX_URL`         | *(unset)* | Plex base URL as reachable from the container, e.g. `https://plex.example.org` or `http://plex:32400`. **Required to enable the Plex step** (both this and `PLEX_TOKEN` must be set). |
| `PLEX_TOKEN`       | *(unset)* | Plex **admin** `X-Plex-Token`. **Required to enable the Plex step.** When either it or `PLEX_URL` is unset, the Plex step is skipped entirely. |
| `PLEX_ACCOUNTS`    | *(admin only)* | Plex Home usernames to select the track for (`user1,user2`), or `*` for all. Subtitle selection is per-account; the admin token resolves each user's token via the Plex Home API (cached). See "…for all users". |
| `PLEX_VERIFY_SSL`  | `1` | Set `0` to skip TLS verification (self-signed cert). |
| `PLEX_TIMEOUT`     | `60` | Seconds to wait for Plex to scan the item / expose the new sidecar stream before giving up. |

Set these in the `shiftarr` service `environment:` in the main compose.

---

## Operating

* **Add a show:** append its folder name to `allowlist.txt` (e.g. `QI`). Takes effect next run.
* **Change the lead / window:** edit `SHIFTARR_LEAD` / `SHIFTARR_SEG_MIN` in the compose env, then
  `docker compose up -d shiftarr` from your compose directory. (Env-only changes don't need a rebuild.)
* **After editing the code:** `docker compose up -d --build shiftarr`.
* **Backfill / re-run manually:** see "Run from the command line" below (the webhook only fires on *new* imports).
* **Logs:** `docker logs shiftarr` or the file at `SHIFTARR_LOG` (e.g. `/config/shiftarr.log`).
* **Disable:** remove/disable the Sonarr "shiftarr" webhook and `docker compose stop shiftarr`.

Sidecars are written as the container's user; if your host uses **userns-remap** (container root =
some host uid), the media and config dirs end up owned by that mapped uid — mount them accordingly.

---

## Run from the command line

The sync is just `python /app/shiftarr.py <video.mkv> [...]` inside the running container, so it
uses the same env (allowlist, `SHIFTARR_LEAD`, etc.) as the webhook. **Paths are container paths**
(e.g. `/tv/...`, whatever your media mount maps to inside the container). It always writes
`<base>.en.srt` (no dry-run).

```bash
# one episode
docker exec shiftarr python /app/shiftarr.py \
  "/tv/Taskmaster/Season 9/Taskmaster - S09E06 - Bready Bready Bready WEBDL-1080p.mkv"

# a whole season
docker exec shiftarr bash -lc 'for f in "/tv/Taskmaster/Season 9"/*.mkv; do python /app/shiftarr.py "$f"; done'

# an entire show (all seasons)
docker exec shiftarr bash -lc 'find "/tv/Taskmaster" -name "*.mkv" | sort | while IFS= read -r f; do python /app/shiftarr.py "$f"; done'

# override a knob for a one-off run (e.g. a different lead) without touching the compose
docker exec -e SHIFTARR_LEAD=-1.5 shiftarr python /app/shiftarr.py "/tv/Taskmaster/Season 4/...mkv"
```

Watch progress with `docker logs -f shiftarr` (or tail the `SHIFTARR_LOG` file).
To sync a show not yet in the allowlist, add it to `allowlist.txt` first (or pass
`-e SHIFTARR_ALLOWLIST=/dev/null` to skip the check for a one-off).

---

## Triggers & related config (outside this repo)

* **Sonarr** → Settings → Connect → Webhook `shiftarr`, On Import + On Upgrade, `POST http://shiftarr:8000/`.
* **Bazarr** → `use_postprocessing: false` and `use_shiftarr: false` (Bazarr no longer syncs;
  this container owns it, using the cleaner embedded source rather than Bazarr's downloads).
* **Plex** → auto-select the synced sidecar per episode via `plex.py` (set `PLEX_TOKEN`; see below).
  The account-level "Subtitle Mode: Always Enabled" + English still works as a fallback, but it can
  pick the *embedded* (drifted) English track instead — the per-item selection below is deterministic.

---

## Auto-select in Plex

By default shiftarr only *writes* `<base>.en.srt`. Plex **does** ingest that sidecar on its own, but it
won't necessarily *select* it: with "Subtitle Mode: Always Enabled" Plex picks *an* English track, and in
practice it grabs whichever it scores highest — often Bazarr's **SDH** `.en.hi.srt` or the drifted
**embedded** track, not shiftarr's clean synced sub. `plex.py` closes that gap: after each sidecar is
written it selects that specific external track for that episode, server-side, so no client config is
needed and neither the SDH nor the embedded track can win.

**Enable it:** set **both** `PLEX_URL` and `PLEX_TOKEN` in the `shiftarr` service env, then
`docker compose up -d shiftarr`. If either is unset the step is a complete no-op — shiftarr behaves as before.

```yaml
    environment:
      - PLEX_URL=https://plex.example.org      # or http://plex:32400
      - PLEX_TOKEN=xxxxxxxxxxxxxxxxxxxx
```

Get a token from any Plex web URL (`&X-Plex-Token=…` in a request), or **Get Info → View XML** on any item.

**How it finds the episode:** the parent folder name (`/tv/<Show>/Season …/file.mkv` → "<Show>") narrows the
candidate Plex shows (loose title match), then the episode is matched by its **path tail** — the last two
components, `Season …/file.mkv`. Those are the real on-disk names, identical on Plex's mount and the
container's `/tv/…` mount, so the mounts needn't line up **and** ambiguous titles like `The Office (UK)` vs
`The Office (US)` still resolve to the right episode (their files differ even though the titles collapse). It
then refreshes that item, waits (up to `PLEX_TIMEOUT`) for the right subtitle stream, and issues
`PUT /library/parts/{part}?subtitleStreamID={id}&allParts=1`.

**Which stream it picks:** Plex marks *external* (sidecar) subtitle streams with a `key` and no `index`, and
reports language as both `languageCode="eng"` and `languageTag="en"`. `plex.py` selects the stream that is
external, matches `SHIFTARR_LANG` (either form), and is not forced — preferring a **clean** track (shiftarr's
`.en.srt`) and only **falling back to SDH** (`.en.hi.srt`) when no clean one exists, so an episode still gets
*English* rather than an embedded foreign default. Plex does *not* expose the sidecar's filename in its API,
so matching is by these attributes, not by path. (Plex otherwise tends to auto-select SDH, or an embedded
`default=1` track — e.g. Swedish — which is what this avoids.)

A Plex failure (wrong token, not scanned yet, show folder ≠ any Plex title) is logged as `PLEX WARN …` and
never fails the sync itself — the sidecar is already on disk. Prerequisite: Plex must scan the episode around
import time (Sonarr → *Connect → Plex Media Server* does this); shiftarr polls for the item but won't create it.

**Backfill an already-synced library** — select existing sidecars in Plex without re-running the sync
(`--plex-only` skips extraction/sync and just does the Plex selection for each video whose `.en.srt` exists):

```bash
# one show
docker exec shiftarr bash -lc 'find "/tv/Taskmaster" -name "*.mkv" | sort | \
  while IFS= read -r f; do python /app/shiftarr.py --plex-only "$f"; done'
```

---

## Known limitation

The **finale** (winner announcement / applause / credits) can overshoot by ~1–2 s: dense applause
biases ffsubsync and there's little clean dialogue to anchor on. It's confined to the last minutes
and is far smaller than the original 4–9 s drift.

---

## History (why it's built this way)

* Global ffsubsync/alass → wrong (single offset can't follow piecewise drift; audio VAD fooled by applause).
* Downloaded subs (OpenSubtitles) → same broadcast timing as embedded; don't help. **Embedded is the cleanest source.**
* Black-frame ad-break detection worked but decoding full 1080p per episode was the whole runtime → dropped for **fixed time windows**.
* Fixed 5-min windows → intros mis-aligned badly (too little dialogue) → **10 min**.
* Absolute search cap → either clipped large real drift or let the first window grab a spurious match → **pre-shift residual** (current design).
