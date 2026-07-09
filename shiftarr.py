#!/usr/bin/env python3
"""
shiftarr.py - extract a video's embedded subtitle and per-segment sync it to the audio.
Runs in the dedicated shiftarr container (ffsubsync + ffmpeg on PATH).

Usage: shiftarr.py <video.mkv> [<video2.mkv> ...]
Writes <base>.<TAG>.srt next to each video.

Method: fixed time-interval windows; per window ffsubsync finds only the residual shift
around the running drift (pre-shift), so total drift accumulates without limit while
spurious far matches are impossible. See the project notes for the derivation.
"""
import sys, os, re, subprocess, tempfile, shutil, datetime, json, statistics
import plex

SEG_MINUTES  = float(os.environ.get("SHIFTARR_SEG_MIN", "10"))   # window length
STEP_CAP     = float(os.environ.get("SHIFTARR_STEP_CAP", "12"))  # residual search bound per window
LEAD         = float(os.environ.get("SHIFTARR_LEAD", "0"))       # lead (s) applied to drift-corrected segments; neg = earlier
SNAP         = float(os.environ.get("SHIFTARR_SNAP", "0.3"))     # per-segment drift below this (s) is treated as 0 (noise)
INTRO_SKIP   = float(os.environ.get("SHIFTARR_INTRO_SKIP", "180"))  # skip the show open in the 1st window (theme/titles mis-align)
MIN_SEG_CUES = 15
ALIGNED_EPS  = 0.4
# --- prototype: refine the step location to the true ad-break cut (audio-only, opt-in) ---
REFINE       = os.environ.get("SHIFTARR_REFINE", "0") not in ("0", "false", "no", "")
REFINE_WIN   = float(os.environ.get("SHIFTARR_REFINE_WIN", "120"))  # centred fit-window length (s)
REFINE_R     = float(os.environ.get("SHIFTARR_REFINE_R", "6")) * 60 # search half-width around boundary (s)
REFINE_NEAR  = float(os.environ.get("SHIFTARR_REFINE_NEAR", "75"))  # search half-width when seeded by a prior (s)
REFINE_MINGAP  = float(os.environ.get("SHIFTARR_REFINE_MINGAP", "6"))    # min cue gap (s) to be a break candidate
REFINE_MINSCORE= float(os.environ.get("SHIFTARR_REFINE_MINSCORE", "50")) # min VAD-overlap advantage (frames)
# Return-from-break keywords: among the cue-gaps near a step, the break is the one whose next line
# contains the most of these (returns say "welcome"/"part two"/"...came back"). Word set, not fixed
# phrases, so wording can vary. Override/extend per show via SHIFTARR_RETURN_WORDS or a "_words" list
# in the cut map; these defaults are what recurs across the corpus.
RETURN_WORDS = os.environ.get("SHIFTARR_RETURN_WORDS",
    "welcome,part,one,two,three,four,five,six,final,semifinal,back")
PRIOR_FILE   = os.environ.get("SHIFTARR_CUTS_FILE", "/config/.shiftarr_cuts.json")  # learned cut map
LANG         = os.environ.get("SHIFTARR_LANG", "eng")            # embedded stream language to prefer
TAG          = os.environ.get("SHIFTARR_TAG", "en")             # sidecar suffix -> <base>.<TAG>.srt
ALLOWLIST    = os.environ.get("SHIFTARR_ALLOWLIST", "/app/allowlist.txt")
LOG          = os.environ.get("SHIFTARR_LOG", "/config/shiftarr.log")

def log(m):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {m}"
    print(line, flush=True)
    try: open(LOG, "a").write(line + "\n")
    except OSError: pass

def sec(t): h,m,r = t.split(":"); s,ms = r.split(","); return int(h)*3600+int(m)*60+int(s)+int(ms)/1000
def ts(x):
    if x < 0: x = 0.0
    h=int(x//3600); x-=h*3600; mm=int(x//60); x-=mm*60; s=int(x); ms=int(round((x-s)*1000))
    if ms == 1000: s += 1; ms = 0
    return f"{h:02d}:{mm:02d}:{s:02d},{ms:03d}"

def run(cmd): return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)

def probe_dur(v): return float(run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",v]).stdout.strip())
def audio_index(v):
    o = run(["ffprobe","-v","error","-select_streams","a","-show_entries","stream=index","-of","csv=p=0",v]).stdout.split()
    return o[0] if o else "1"
def sub_index(v):
    o = run(["ffprobe","-v","error","-select_streams","s","-show_entries",
             "stream=index,codec_name:stream_tags=language","-of","csv=p=0",v]).stdout
    untagged = None
    for ln in o.splitlines():
        p = ln.split(",")
        if len(p) >= 2 and p[1] == "subrip":
            lang = (p[2].strip() if len(p) > 2 else "")
            if lang == LANG: return p[0]                      # exact language match wins
            if untagged is None and lang in ("", "und"):      # untagged -> assume target language
                untagged = p[0]
            # a stream tagged as a *different* language (e.g. swe) is never used, so we don't
            # extract e.g. Swedish and mislabel it <base>.en.srt
    return untagged

def extract_wav(v, aidx, t0, t1, path):
    run(["ffmpeg","-nostdin","-y","-v","error","-ss",str(t0),"-to",str(t1),"-i",v,
         "-map",f"0:{aidx}","-ac","1","-ar","16000",path])

def ffsubsync_offset(wav, cues_rel, tmp, tag, max_off):
    if len(cues_rel) < 8: return None
    ins, outs = f"{tmp}/{tag}.srt", f"{tmp}/{tag}.out.srt"
    with open(ins,"w") as f:
        for i,(a,b) in enumerate(cues_rel,1): f.write(f"{i}\n{ts(a)} --> {ts(b)}\nx\n\n")
    r = run(["ffsubsync",wav,"-i",ins,"-o",outs,"--no-fix-framerate","--max-offset-seconds",str(max_off)])
    m = re.search(r"offset seconds:\s*(-?[0-9.]+)", (r.stderr or "") + (r.stdout or ""))
    return float(m.group(1)) if m else None

# --- prototype: locate the true ad-break end near a fixed boundary, from audio VAD (in-process) ---
def _speech(wav):
    """ffsubsync's tuned/smoothed VAD speech signal at 100 fps for a wav. Imported lazily (and
    guarded) so the module still loads and non-refine runs work even if ffsubsync internals change."""
    import logging, contextlib, numpy as np
    from ffsubsync.speech_transformers import VideoSpeechTransformer
    from ffsubsync import constants as C
    with open(os.devnull, "w") as dn, contextlib.redirect_stderr(dn):
        logging.disable(logging.CRITICAL)
        try:
            vst = VideoSpeechTransformer(vad=C.DEFAULT_VAD, sample_rate=C.SAMPLE_RATE,
                                         frame_rate=48000, non_speech_label=0.0, start_seconds=0)
            vst.fit(wav)
        finally:
            logging.disable(logging.NOTSET)
    return np.asarray(vst.video_speech_results_, float).ravel()

def return_words(extra=None):
    """Return-keyword set (defaults + any per-show words from the cut map's '_words')."""
    ws = set(w.strip().lower() for w in RETURN_WORDS.split(",") if w.strip())
    ws.update(w.strip().lower() for w in (extra or []) if w.strip())
    return ws

def _return_hits(text, words):
    """How many return keywords appear in a cue's text."""
    return len(set(re.findall(r"[a-z]+", (text or "").lower())) & words)

def _cue_gaps(cues, t0, t1, min_gap):
    """Break candidates = the start time of a cue that follows a >= min_gap silence in the cue
    timeline, within [t0,t1]. An ad break leaves such a gap (no dialogue during the removed
    bumper/break); these are far cleaner and fewer than audio-silence candidates."""
    return [cues[i][0] for i in range(1, len(cues))
            if cues[i][0] - cues[i-1][1] >= min_gap and t0 <= cues[i][0] <= t1]

def refine_cut(video, aidx, cues, b, off_lo, off_hi, tmp, tag, prior=None, ctext=None, rwords=None):
    """Locate the true ad-break end near fixed boundary `b` (where drift steps off_lo -> off_hi).
    Candidates are the cue-gaps in the search window; we keep the gap(s) whose following line has the
    most return keywords ("welcome"/"part"/"back"...), then pick the one where the audio clearly
    switches from fitting off_lo (before) to off_hi (after), scored by VAD-speech overlap. Evaluating
    only at real gaps (never a continuous argmax) avoids spurious peaks. Forward-biased from the grid
    line, tightened around a season `prior` when present. Returns the cut (cue-timeline), or None."""
    import numpy as np
    lo_t = (prior - REFINE_NEAR) if prior else (b - 60.0)      # cold: from just behind the grid line
    hi_t = (prior + REFINE_NEAR) if prior else (b + REFINE_R)  # ... forward to +R
    cands = _cue_gaps(cues, lo_t, hi_t, REFINE_MINGAP)
    if not cands: return None
    if ctext and rwords:                            # keep the gap(s) with the most return keywords
        hits = {g: _return_hits(ctext.get(g, ""), rwords) for g in cands}
        top = max(hits.values())
        if top > 0: cands = [g for g in cands if hits[g] == top]
    rt0 = max(0.0, lo_t - REFINE_WIN); rt1 = hi_t + REFINE_WIN
    wav = f"{tmp}/{tag}.wav"; extract_wav(video, aidx, rt0, rt1, wav)
    try:
        av = _speech(wav)
    except Exception as e:
        log(f"  refine {tag}: VAD unavailable ({e}) -> keep fixed"); return None
    n = len(av); H = REFINE_WIN / 2.0
    def ov(a0, a1, off):                    # speech-overlap of cues in [a0,a1) shifted by off
        tot = 0.0
        for a, bb in cues:
            if a < a0 or a >= a1: continue
            st = int(round((a+off-rt0)*100)); en = int(round((bb+off-rt0)*100))
            if en > 0 and st < n: tot += float(av[max(0,st):min(n,en)].sum())
        return tot
    best = None                              # gap that best fits off_lo before / off_hi after
    for g in cands:
        s = (ov(g-H, g, off_lo) - ov(g-H, g, off_hi)) + (ov(g, g+H, off_hi) - ov(g, g+H, off_lo))
        if best is None or s > best[1]: best = (g, s)
    if best is None or best[1] <= REFINE_MINSCORE: return None
    return best[0]


def _prior_key(video):
    """Prior is keyed per season, not per whole series -- episode length (and so ad-break spacing)
    changes between seasons. e.g. 'Taskmaster/Season 6'."""
    d = os.path.dirname(video)
    return f"{os.path.basename(os.path.dirname(d))}/{os.path.basename(d)}"

def load_priors():
    try: return json.load(open(PRIOR_FILE))
    except Exception: return {}

def save_priors(p):
    try: json.dump(p, open(PRIOR_FILE, "w"))
    except OSError: pass

def learn_words(videos):
    """Mine per-series return-from-break keywords: alphabetic words that are over-represented in the
    cue *following* a gap (vs. all cues) and recur across >=2 episodes -- 'welcome'/'part'/'back'...
    Written to the cut map under the series key's '_words', extending the built-in defaults."""
    from collections import Counter
    priors = load_priors(); by_series = {}
    for v in videos:
        by_series.setdefault(_prior_key(v).split("/")[0], []).append(v)
    for series, vids in by_series.items():
        after, allw, eps = Counter(), Counter(), {}
        for v in vids:
            # read the already-written sidecar (KB) rather than demuxing the whole mkv (GB) --
            # subtitle extraction streams through the entire interleaved container.
            sc = english_sidecar(os.path.splitext(v)[0])
            if not sc:
                continue
            cues = []
            for blk in re.split(r"\n\s*\n", open(sc, encoding="utf-8", errors="ignore").read()):
                m = re.search(r"(\d\d:\d\d:\d\d,\d+)\s*-->\s*(\d\d:\d\d:\d\d,\d+)(.*)", blk, re.S)
                if m: cues.append((sec(m.group(1)), sec(m.group(2)), " ".join(m.group(3).split())))
            if not cues: continue
            endt = cues[-1][0]                       # skip opening/closing credits (non-dialogue text)
            for i in range(len(cues)):
                w = set(re.findall(r"[a-z]{3,}", cues[i][2].lower()))
                allw.update(w)
                if i > 0 and cues[i][0] - cues[i-1][1] >= REFINE_MINGAP and 0.03*endt < cues[i][0] < 0.93*endt:
                    after.update(w)
                    for x in w: eps.setdefault(x, set()).add(v)
        # a return word recurs across episodes (many eps) AND mostly appears after gaps (high fraction) --
        # this favours "welcome/part/back" over rare SDH sound-effects and over ubiquitous filler words.
        scored = [(len(eps[w]) * ca / max(allw[w], 1), w) for w, ca in after.items() if len(eps.get(w, ())) >= 3]
        words = [w for sc, w in sorted(scored, reverse=True) if sc > 1.0][:15]
        priors.setdefault(series, {})["_words"] = words
        log(f"LEARN {series}: {len(vids)} eps -> return-words {words}")
    save_priors(priors)

def english_sidecar(base):
    """Existing external sub in language LANG to sync/select, preferring a clean track over SDH.
    Used when the video has no embedded LANG subtitle (shiftarr then syncs this one instead)."""
    for cand in (f"{base}.{TAG}.srt", f"{base}.{TAG}.hi.srt"):
        if os.path.exists(cand):
            return cand
    return None

def process(video):
    if os.path.exists(ALLOWLIST):
        pats = [l.strip() for l in open(ALLOWLIST) if l.strip() and not l.startswith("#")]
        if pats and not any(p in video for p in pats):
            log(f"SKIP not in allowlist: {os.path.basename(video)}"); return None
    if not os.path.exists(video):
        log(f"ERROR missing: {video}"); return None
    base = os.path.splitext(video)[0]; out = f"{base}.{TAG}.srt"
    sidx = sub_index(video)
    if sidx:                                     # embedded LANG sub: extract it, then sync
        if run(["ffmpeg","-nostdin","-y","-v","error","-i",video,"-map",f"0:{sidx}",out]).returncode != 0 \
                or not os.path.exists(out):
            log(f"ERROR extract failed: {os.path.basename(video)}"); return None
    else:                                        # no embedded LANG sub: sync an existing external
        out = english_sidecar(base)              # e.g. Bazarr's .en.srt or SDH .en.hi.srt
        if not out:
            log(f"SKIP no embedded or external {LANG} sub: {os.path.basename(video)}"); return None
        orig = out + ".orig"
        if not os.path.exists(orig):
            shutil.copyfile(out, orig)           # one-time backup of the un-synced source
        log(f"no embedded {LANG} sub; syncing external {os.path.basename(out)} in place")

    dur = probe_dur(video); aidx = audio_index(video)
    seg_s = SEG_MINUTES*60
    edges = [i*seg_s for i in range(max(1, int(dur//seg_s)+1))]
    if edges[-1] >= dur - seg_s*0.5: edges[-1] = dur
    else: edges.append(dur)
    edges = sorted({min(e, dur) for e in edges}); segs = list(zip(edges[:-1], edges[1:]))
    txt = open(out, encoding="utf-8", errors="ignore").read()
    cues = [(sec(a), sec(b)) for a,b in re.findall(r"(\d\d:\d\d:\d\d,\d+)\s*-->\s*(\d\d:\d\d:\d\d,\d+)", txt)]
    ctext = {}                                       # {cue-start-seconds: text} for return-phrase matching
    for blk in re.split(r"\n\s*\n", txt):
        m = re.search(r"(\d\d:\d\d:\d\d,\d+)\s*-->\s*\d\d:\d\d:\d\d,\d+(.*)", blk, re.S)
        if m: ctext[sec(m.group(1))] = " ".join(m.group(2).split())
    log(f"START {os.path.basename(video)} dur={dur:.0f}s win={seg_s/60:.0f}m segs={len(segs)} cues={len(cues)}")

    tmp = tempfile.mkdtemp(); seg_offsets = []; prev = 0.0
    for i,(t0,t1) in enumerate(segs,1):
        a0 = t0 + INTRO_SKIP if i == 1 else t0    # skip the show open in the first window (mis-aligns badly)
        segcues = [(a,b) for a,b in cues if a0 <= a < t1]
        shifted = [(a-a0+prev, b-a0+prev) for a,b in segcues if (a-a0+prev) >= 0]
        if len(shifted) < MIN_SEG_CUES:
            seg_offsets.append(prev); log(f"  seg{i} {t0/60:.1f}-{t1/60:.1f}m: few cues -> inherit {prev:+.2f}"); continue
        wav = f"{tmp}/s{i}.wav"; extract_wav(video, aidx, a0, t1, wav)
        delta = ffsubsync_offset(wav, shifted, tmp, f"s{i}", STEP_CAP)
        if delta is None:
            seg_offsets.append(prev); log(f"  seg{i} {t0/60:.1f}-{t1/60:.1f}m: no estimate -> inherit {prev:+.2f}")
        else:
            off = prev + delta; seg_offsets.append(off); prev = off
            log(f"  seg{i} {t0/60:.1f}-{t1/60:.1f}m: {off:+.2f}s (delta {delta:+.2f})")

    # the lead rides ONLY segments that actually drifted (>= SNAP); a segment within noise stays put
    # (no drift shift and no lead), so an already-accurate sub is left exactly as-is.
    applied = [(o + LEAD) if abs(o) >= SNAP else 0.0 for o in seg_offsets]
    if all(a == 0.0 for a in applied):
        log(f"RESULT already aligned -> no change ({os.path.basename(out)})")
        shutil.rmtree(tmp, ignore_errors=True); return out

    if REFINE:
        # move each step from the fixed grid to the true ad-break cut; a per-season learned cut map
        # (PRIOR_FILE) seeds the search near where the break usually falls, and records what we find.
        key = _prior_key(video); priors = load_priors(); kd = priors.setdefault(key, {})
        series_words = (priors.get(key.split("/")[0], {}) or {}).get("_words")
        rwords = return_words(series_words)              # defaults + mined per-series words
        bounds = [t1 for _, t1 in segs]
        for i in range(len(segs) - 1):
            d = seg_offsets[i+1] - seg_offsets[i]
            if abs(d) < ALIGNED_EPS:
                continue                            # no real step here -> nothing to move
            rec = kd.get(str(i), [])
            prior = statistics.median(rec) if len(rec) >= 1 else None
            c = refine_cut(video, aidx, cues, segs[i][1], seg_offsets[i], seg_offsets[i+1], tmp, f"r{i}", prior, ctext, rwords)
            seeded = prior is not None and c is not None
            if c is None and prior is not None:     # prior didn't bracket this episode -> retry wide
                c = refine_cut(video, aidx, cues, segs[i][1], seg_offsets[i], seg_offsets[i+1], tmp, f"r{i}w", None, ctext, rwords)
            if c is not None:
                bounds[i] = c
                kd[str(i)] = (rec + [round(c, 1)])[-9:]     # keep the last few for a rolling median
                log(f"  refine seg{i+1}->seg{i+2}: {segs[i][1]/60:.1f}m -> cut {c/60:.2f}m "
                    f"(step {d:+.2f}{'; seeded' if seeded else ''})")
        save_priors(priors)
        def shift_for(t):
            for bnd, off in zip(bounds, applied):    # `applied` already carries the lead on drifted segs
                if t < bnd: return off
            return applied[-1]
    else:
        def shift_for(t):
            acc = 0.0
            for (t0,t1),off in zip(segs, applied):
                if t < t1 + acc: return off
                acc = off
            return applied[-1]
    newtxt = re.sub(r"(\d\d:\d\d:\d\d,\d+)\s*-->\s*(\d\d:\d\d:\d\d,\d+)",
                    lambda m: (lambda s: f"{ts(sec(m.group(1))+s)} --> {ts(sec(m.group(2))+s)}")(shift_for(sec(m.group(1)))),
                    txt)
    open(out, "w", encoding="utf-8").write(newtxt)
    log(f"RESULT shifts={[round(o,2) for o in seg_offsets]} lead={LEAD:+.2f} -> {os.path.basename(out)}")
    shutil.rmtree(tmp, ignore_errors=True)
    return out

def do_plex(v, out):
    if not (out and plex.enabled()):
        return
    try:
        sid = plex.force_subtitle(v, out, log)
        if sid: log(f"PLEX selected external sub stream {sid} for {os.path.basename(v)}")
    except Exception as e:
        log(f"PLEX WARN {os.path.basename(v)}: {e}")

if __name__ == "__main__":
    args = sys.argv[1:]
    videos = [a for a in args if not a.startswith("--")]
    if "--learn-words" in args:                  # mine per-series return-from-break keywords, then exit
        learn_words(videos); sys.exit(0)
    plex_only = "--plex-only" in args            # backfill: don't re-sync, just select the existing sidecar
    if plex_only and not plex.enabled():
        log("ERROR --plex-only needs PLEX_URL and PLEX_TOKEN set"); sys.exit(2)
    for v in videos:
        if plex_only:
            out = english_sidecar(os.path.splitext(v)[0])
            if not out:
                log(f"PLEX SKIP no sidecar: {os.path.basename(v)}"); continue
        else:
            try: out = process(v)
            except Exception as e: log(f"ERROR {os.path.basename(v)}: {e}"); continue
        do_plex(v, out)
