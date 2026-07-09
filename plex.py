#!/usr/bin/env python3
"""
Optional Plex integration for shiftarr (stdlib only, no extra pip deps).

After shiftarr writes <base>.<TAG>.srt, force Plex to *select* that external sidecar
as the episode's subtitle track, server-side and per item, so you no longer pick it
by hand -- and Plex can never fall back to the drifted *embedded* English track.

No-op unless both PLEX_URL and PLEX_TOKEN are set (so on a box without Plex config
this module does nothing and never raises).

Flow (see force_subtitle):
  1. locate the episode in Plex by matching the video's *filename* against the
     episodes of the show named by the parent folder (same file on disk -> same
     basename, regardless of how Plex mounts the library);
  2. refresh that item's metadata so Plex ingests the freshly written sidecar;
  3. poll until the external subtitle stream (matching the sidecar filename) appears;
  4. PUT /library/parts/{partID}?subtitleStreamID={id}&allParts=1 to select it.
"""
import os, re, ssl, time, json, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

PLEX_URL   = os.environ.get("PLEX_URL", "").rstrip("/")    # e.g. https://plex.example.org (required)
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")              # admin X-Plex-Token (required; used for reads)
# Subtitle selection is stored per Plex account. Configure it by *username*: PLEX_ACCOUNTS is a
# comma-separated list of Plex Home account titles (e.g. "user1,user2") to select the track
# for, or "*" for every home user. Empty -> admin only. The admin token resolves each user's PMS
# token via the Plex Home API (switch -> resources accessToken), cached to PLEX_TOKEN_CACHE so a
# backfill doesn't re-hit plex.tv. Include or omit the admin's own username to select it or not.
# Reads/find/refresh always use the admin PLEX_TOKEN.
PLEX_ACCOUNTS = os.environ.get("PLEX_ACCOUNTS", "")
PLEX_CID      = os.environ.get("PLEX_CLIENT_ID", "shiftarr-plex-select")
_TOKEN_CACHE  = os.environ.get("PLEX_TOKEN_CACHE", "/config/.plex_select_tokens.json")
LANG       = os.environ.get("SHIFTARR_LANG", "eng")         # 3-letter code Plex reports (languageCode)
VERIFY_SSL = os.environ.get("PLEX_VERIFY_SSL", "1") not in ("0", "false", "False", "no", "")
POLL_SECS  = float(os.environ.get("PLEX_TIMEOUT", "60"))   # wait budget for item/stream to appear
_REQ_TO    = float(os.environ.get("PLEX_REQ_TIMEOUT", "15"))

_ctx = None
if PLEX_URL.startswith("https") and not VERIFY_SSL:
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE


def enabled():
    return bool(PLEX_URL and PLEX_TOKEN)


def _req(method, path, params=None, token=None):
    url = PLEX_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Plex-Token", token or PLEX_TOKEN)
    req.add_header("Accept", "application/xml")
    with urllib.request.urlopen(req, timeout=_REQ_TO, context=_ctx) as r:
        return r.read()


def _plextv(method, path, token=None):
    """Call a plex.tv account endpoint (needs a client-identifier)."""
    req = urllib.request.Request("https://plex.tv" + path, method=method)
    req.add_header("X-Plex-Token", token or PLEX_TOKEN)
    req.add_header("X-Plex-Client-Identifier", PLEX_CID)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=_REQ_TO) as r:
        return json.load(r)


def _server_token(user_auth, server_id):
    """Exchange a Home-user switch token for *this server's* long-lived access token (the switch
    token isn't valid against the PMS directly; it must be claimed for the server via resources)."""
    for dev in _plextv("GET", "/api/v2/resources?includeHttps=1", token=user_auth):
        if dev.get("clientIdentifier") == server_id:
            return dev.get("accessToken")
    return None


def select_tokens(log=lambda *_: None):
    """PMS tokens to apply the selection with -- one per username in PLEX_ACCOUNTS (title match, or
    "*" for all home users), resolved from the admin token and cached to _TOKEN_CACHE so a backfill
    doesn't re-hit plex.tv per episode. Empty PLEX_ACCOUNTS -> admin only. Falls back to the admin
    token on any failure so selection still happens."""
    want = PLEX_ACCOUNTS.strip()
    if not want:
        return [PLEX_TOKEN]
    names = None if want in ("*", "all") else [n.strip().lower() for n in want.split(",") if n.strip()]
    try:
        cache = json.load(open(_TOKEN_CACHE))
    except Exception:
        cache = {}
    if names is not None and all(n in cache for n in names):     # fast path: all cached, no plex.tv
        return [cache[n] for n in names]
    try:
        users = _plextv("GET", "/api/v2/home/users").get("users", [])
        server_id = ET.fromstring(_req("GET", "/")).get("machineIdentifier")
    except Exception as e:
        log(f"PLEX WARN home-users lookup failed ({e}); selecting admin only")
        return [PLEX_TOKEN]
    tokens, changed = [], False
    for u in users:
        title = (u.get("title") or "")
        if names is not None and title.lower() not in names:
            continue
        tok = cache.get(title.lower())
        if not tok:
            try:
                auth = _plextv("POST", f"/api/v2/home/users/{u.get('uuid')}/switch").get("authToken")
                tok = _server_token(auth, server_id) if auth else None
            except Exception as e:
                log(f"PLEX WARN could not resolve token for {title!r}: {e}"); continue
            if tok:
                cache[title.lower()] = tok; changed = True
        if tok:
            tokens.append(tok)
    if changed:
        try: json.dump(cache, open(_TOKEN_CACHE, "w"))
        except Exception: pass
    return tokens or [PLEX_TOKEN]


def _norm(s):
    """Normalise a title/folder for comparison: drop parentheticals like (2015)/(US),
    then keep alnum only -- so 'The Office (US)' == 'The Office' and punctuation/case differ freely."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\([^)]*\)", "", s or "").lower())


def _show_sections():
    root = ET.fromstring(_req("GET", "/library/sections"))
    return [d.get("key") for d in root.findall("Directory") if d.get("type") == "show"]


def _tail(path):
    """Last two path components ('Season 3/Episode.mkv') -- identical on Plex's mount and the
    container's mount (same files on disk), and unique across shows, so it disambiguates even
    'The Office (UK)' vs 'The Office (US)' where the show *title* is ambiguous."""
    return os.path.join(os.path.basename(os.path.dirname(path)), os.path.basename(path))


def _find_episode(video):
    """Return (episodeRatingKey, partID) for the episode file `video`, or (None, None).

    The show *title* only narrows the candidate shows (folder name -> Plex title, loose match);
    the actual match is on the episode's path tail, so ambiguous titles still resolve correctly.
    """
    want = _tail(video)
    target = _norm(os.path.basename(os.path.dirname(os.path.dirname(video))))
    if not target:
        return None, None
    for sec in _show_sections():
        try:
            shows = ET.fromstring(_req("GET", f"/library/sections/{sec}/all", {"type": 2}))
        except Exception:
            continue                                  # one flaky section shouldn't abort the search
        for d in shows.findall("Directory"):
            if _norm(d.get("title")) != target:
                continue
            try:
                leaves = ET.fromstring(_req("GET", f"/library/metadata/{d.get('ratingKey')}/allLeaves"))
            except Exception:
                continue
            for vid in leaves.findall("Video"):
                for part in vid.iter("Part"):
                    if _tail(part.get("file") or "") == want:
                        return vid.get("ratingKey"), part.get("id")
    return None, None


def _external_sub_id(rating_key, log=lambda *_: None):
    """streamID of the external sidecar subtitle to select: an *external* subtitle stream
    (Plex marks these with a `key` and no `index`) in language LANG. Prefer a clean sub
    (shiftarr's `.<TAG>.srt`); fall back to an SDH one (Bazarr's `.<TAG>.hi.srt`) only when no
    clean one exists -- so an episode with no clean English source still gets *English*, never
    the embedded non-English default track.

    Plex does not expose the sidecar's filename in the metadata response, so we can't match by
    path; we match by these stream attributes instead. Ties -> last (most-recently-added).
    """
    root = ET.fromstring(_req("GET", f"/library/metadata/{rating_key}"))
    clean, sdh = [], []
    for st in root.iter("Stream"):
        if st.get("streamType") != "3" or not st.get("key"):     # subtitles; external only
            continue
        # Plex reports both languageCode="eng" (ISO 639-2) and languageTag="en" (639-1);
        # accept LANG in either form so SHIFTARR_LANG=eng and =en both work.
        if LANG not in (st.get("languageCode"), st.get("languageTag")):
            continue
        if st.get("forced") == "1":
            continue
        (sdh if st.get("hearingImpaired") == "1" else clean).append(st.get("id"))
    pool = clean or sdh
    if not clean and sdh:
        log(f"PLEX note: no clean {LANG} sub on item {rating_key}; falling back to SDH {sdh[-1]}")
    elif len(pool) > 1:
        log(f"PLEX note: {len(pool)} clean {LANG} subs on item {rating_key}; picking {pool[-1]}")
    return pool[-1] if pool else None


def force_subtitle(video, sidecar, log=lambda *_: None):
    """Make Plex select `sidecar` for `video`. Returns the streamID set, or None (no-op)."""
    if not enabled():
        return None
    if not os.path.exists(sidecar):
        return None

    deadline = time.time() + POLL_SECS
    rk = part = None
    while True:
        rk, part = _find_episode(video)
        if rk or time.time() >= deadline:
            break
        time.sleep(4)
    if not rk:
        raise RuntimeError(f"episode not found in Plex within {POLL_SECS:.0f}s (not scanned yet?)")

    log(f"PLEX found ratingKey={rk} part={part}")

    # The sidecar is normally already ingested, so look for it WITHOUT refreshing first.
    # A metadata refresh resets the item's selected subtitle to the file's *default* track
    # (here an embedded Swedish one, default=1) and races with our selection below -- so we
    # only refresh as a fallback when the stream isn't visible yet (a brand-new import).
    sid = _external_sub_id(rk, log)
    if not sid:
        _req("PUT", f"/library/metadata/{rk}/refresh")   # nudge ingest of a just-written sidecar
        while not sid and time.time() < deadline:
            time.sleep(4)
            sid = _external_sub_id(rk, log)
    if not sid:
        raise RuntimeError(f"no external {LANG} (non-SDH) subtitle on the item in Plex")

    # Select it for every configured account (selection is per-user), then confirm it stuck for
    # the admin -- retry in case a concurrent refresh (Sonarr/Plex) resets it to the default track.
    tokens = select_tokens(log)
    for _ in range(3):
        for tok in tokens:
            _req("PUT", f"/library/parts/{part}", {"subtitleStreamID": sid, "allParts": 1}, token=tok)
        if _is_selected(rk, sid):
            if len(tokens) > 1:
                log(f"PLEX selected stream {sid} for {len(tokens)} accounts")
            return sid
        time.sleep(2)
    raise RuntimeError(f"selected stream {sid} did not stick (concurrent refresh resetting it?)")


def _is_selected(rating_key, sid):
    root = ET.fromstring(_req("GET", f"/library/metadata/{rating_key}"))
    return any(st.get("id") == sid and st.get("selected") == "1" for st in root.iter("Stream"))
