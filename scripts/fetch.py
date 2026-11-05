"""
NIGHTDECK library builder. Runs inside GitHub Actions (open internet).

tracks.json  ->  audio/*.mp3 (112 kbps), manifest.json, CREDITS.md, fetch-report.json,
                 mastered.json

- Every track gets the same finishing pass (see master()): silence trimmed
  off both ends, a gentle shared EQ, and two-pass loudness normalization,
  so the whole station sits at one level and one warm tone. Each track's
  ending is measured too, and the manifest's "xfade" says how many seconds
  before the end the player should start crossfading into the next track.
- mastered.json records which files have had the current pass (MASTER).
  Bump MASTER to redo every track from its original download; a track whose
  source has gone away is redone from the copy already here instead.
- Already-finished tracks are skipped, so re-runs are cheap. A run stops
  taking on new work after BUDGET_MIN minutes, commits, and the workflow
  starts another run to carry on.
- A track removed from tracks.json has its audio file deleted (that's the purge).
- Each source is tried in order: a direct .mp3 link, or a Free Music Archive /
  Bandcamp page the audio link is scraped from.
"""
import html, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def get(url, binary=False, referer=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Referer": referer} if referer else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def fma_audio(page, title):
    """FMA track pages carry the player data in data-track-info JSON."""
    raw = get(page)
    infos = []
    for m in re.finditer(r'data-track-info="([^"]+)"', raw):
        try:
            infos.append(json.loads(html.unescape(m.group(1))))
        except Exception:
            pass
    want = page.rstrip("/").split("/")[-1].lower()
    for info in infos:
        u = info.get("fileUrl") or info.get("playbackUrl") or ""
        if u and want in (info.get("url") or "").lower():
            return u
    for info in infos:
        if (info.get("title") or "").strip().lower() == title.strip().lower():
            u = info.get("fileUrl") or info.get("playbackUrl")
            if u:
                return u
    flat = html.unescape(raw).replace("\\/", "/")
    hits = re.findall(r'https://files\.freemusicarchive\.org/[^"\'\s<>]+?\.mp3', flat)
    return hits[0] if hits else None


def bandcamp_audio(page):
    flat = html.unescape(get(page)).replace("\\/", "/")
    m = re.search(r'"mp3-128"\s*:\s*"([^"]+)"', flat)
    return m.group(1) if m else None


def resolve(src, title):
    if re.search(r"\.mp3(\?|$)", src, re.I):
        return src
    if "freemusicarchive.org" in src:
        return fma_audio(src, title)
    if "bandcamp.com" in src:
        return bandcamp_audio(src)
    # ccMixter and other plain pages: take the first .mp3 link on the page.
    flat = html.unescape(get(src)).replace("\\/", "/")
    hits = re.findall(r'(https?://[^"\'\s<>]+?\.mp3)', flat)
    if not hits:
        rel = re.findall(r'href="(/[^"\s<>]+?\.mp3)"', flat)
        if rel:
            from urllib.parse import urljoin
            hits = [urljoin(src, rel[0])]
    return hits[0] if hits else None


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


# ── the finishing pass ──────────────────────────────────────────────
MASTER = 1                       # bump to redo every track
LUFS, TRUE_PEAK, LRA = -16.0, -1.5, 11.0
TRIM = "silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.05"
EQ = ",".join([
    "highpass=f=28:poles=2",                     # rumble nobody can hear
    "equalizer=f=250:t=q:w=1.0:g=-1.5",          # a little low-mid mud
    "equalizer=f=3800:t=q:w=1.4:g=-1.0",         # edge off harsh upper mids
    "highshelf=f=9500:g=-1.5",                   # softer, warmer top
])


def master(src, dst, t, bitrate, tmp):
    """Trim silence, EQ, normalize loudness (two passes), encode."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    mid = tmp / "mid.wav"
    # 1. trim both ends (reverse, trim, reverse back) and EQ, kept as float
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-vn",
                    "-af", f"{TRIM},areverse,{TRIM},areverse,{EQ}",
                    "-ar", "44100", "-c:a", "pcm_f32le", str(mid)], check=True)
    # 2. measure loudness
    ln = f"loudnorm=I={LUFS}:TP={TRUE_PEAK}:LRA={LRA}"
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(mid),
                          "-af", ln + ":print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    m = json.loads(out[out.rindex("{"):out.rindex("}") + 1])
    # 3. apply it and encode
    ln += (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
           f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mid), "-af", ln,
                    "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100",
                    "-metadata", f"title={t['title']}", "-metadata", f"artist={t['artist']}",
                    "-metadata", f"comment={t['license']} {t['page']}", str(dst)], check=True)
    mid.unlink(missing_ok=True)
    return round(float(m["input_i"]), 1)


def crossfade_point(path):
    """Seconds before the end where the player should start the next track:
    where the ending drops 10 dB below the body of the track, plus a little
    lead-in, held between 4 and 10 seconds."""
    from array import array
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    a = array("h"); a.frombytes(pcm[: len(pcm) // 2 * 2])
    win = 4000                                    # half a second
    levels = []
    for i in range(0, len(a) - win + 1, win):
        ms = sum(x * x for x in a[i:i + win]) / win
        levels.append(10 * __import__("math").log10(ms / 32768 ** 2 + 1e-12))
    if len(levels) < 8:
        return 5.0
    heard = sorted(l for l in levels if l > -60) or [-20.0]
    body = heard[int(len(heard) * 0.6)]
    k = max((i for i, l in enumerate(levels) if l >= body - 10), default=len(levels) - 1)
    tail = len(a) / 8000 - (k + 1) * 0.5
    return round(min(10.0, max(4.0, tail + 2.5)), 1)


def main():
    started = time.time()
    budget = float(os.environ.get("BUDGET_MIN", "150")) * 60
    state_path = ROOT / "mastered.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    spec = json.loads((ROOT / "tracks.json").read_text())
    lib, bitrate, tracks = spec["library"], spec.get("bitrate", "112k"), spec["tracks"]
    report, manifest = [], []
    wanted = {t["file"] for t in tracks}

    # Purge: anything in audio/ that is no longer listed.
    purged = []
    for f in sorted((ROOT / "audio").glob("*.mp3")) if (ROOT / "audio").exists() else []:
        rel = f"audio/{f.name}"
        if rel not in wanted:
            f.unlink()
            purged.append(rel)

    state = {k: v for k, v in state.items() if k in wanted}
    tmp = Path(tempfile.mkdtemp())
    for t in tracks:
        dst = ROOT / t["file"]
        entry = {"id": t["id"], "title": t["title"], "artist": t["artist"]}
        have = dst.exists() and duration(dst) > 20
        done = have and (state.get(t["file"]) or {}).get("v") == MASTER
        if done:
            entry["status"] = "kept"
        elif time.time() - started > budget:
            entry["status"] = "pending" if have else "waiting"   # next run picks these up
        else:
            entry["status"] = "failed"
            entry["tried"] = []
            for src in t["sources"]:
                try:
                    url = resolve(src, t["title"])
                    if not url:
                        entry["tried"].append({"source": src, "error": "no audio link found on page"})
                        continue
                    raw = tmp / "in.bin"
                    raw.write_bytes(get(url, binary=True, referer=src))
                    if duration(raw) < 20:
                        entry["tried"].append({"source": src, "error": f"not audio or too short ({raw.stat().st_size} bytes)"})
                        continue
                    lufs = master(raw, dst, t, bitrate, tmp)
                    entry.update(status="mastered" if have else "downloaded", from_url=url, was_lufs=lufs)
                    break
                except Exception as e:
                    entry["tried"].append({"source": src, "error": f"{type(e).__name__}: {e}"[:300]})
                time.sleep(1)
            if entry["status"] == "failed" and have:
                # the original is gone; finish the copy we already have
                try:
                    keep = tmp / "old.mp3"
                    shutil.copy(dst, keep)
                    entry.update(status="mastered", from_url="existing copy",
                                 was_lufs=master(keep, dst, t, bitrate, tmp))
                except Exception as e:
                    entry["tried"].append({"source": "existing copy", "error": f"{type(e).__name__}: {e}"[:300]})
            if entry["status"] in ("downloaded", "mastered"):
                entry.pop("tried")
                state[t["file"]] = {"v": MASTER, "xfade": crossfade_point(dst)}
        if dst.exists():
            entry["seconds"] = round(duration(dst))
            st = state.get(t["file"]) or {}
            if "xfade" not in st and entry["status"] == "kept":
                st = state[t["file"]] = {**st, "xfade": crossfade_point(dst)}
            manifest.append({
                "id": t["id"], "url": t["file"], "title": t["title"], "artist": t["artist"],
                "library": lib, "license": t["license"], "license_url": t["license_url"],
                "youtube_ok": t["youtube_ok"], "page": t["page"], "seconds": entry["seconds"],
                **({"xfade": st["xfade"]} if "xfade" in st else {}),
            })
        report.append(entry)
        print(f"[{entry['status']:>10}] {t['artist']} - {t['title']}", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    state_path.write_text(json.dumps(dict(sorted(state.items())), indent=1) + "\n")

    (ROOT / "manifest.json").write_text(json.dumps(
        {"library": lib, "tracks": manifest}, indent=2, ensure_ascii=False) + "\n")

    lines = [f"# Credits — NIGHTDECK `{lib}` library", "",
             "All music here is Creative Commons or public domain. Licenses are per track.",
             "Tracks marked **radio only** are NonCommercial and/or NoDerivatives: fine for the",
             "free web radio, not for monetized or video uploads.", ""]
    for m in manifest:
        tag = "" if m["youtube_ok"] else " — **radio only**"
        lines.append(f"- “{m['title']}” by {m['artist']} — [{m['license']}]({m['license_url']}) — "
                     f"[source]({m['page']}){tag}")
    (ROOT / "CREDITS.md").write_text("\n".join(lines) + "\n")

    size = sum(f.stat().st_size for f in (ROOT / "audio").glob("*.mp3")) if (ROOT / "audio").exists() else 0
    summary = {
        "library": lib, "listed": len(tracks), "in_library": len(manifest),
        "downloaded": sum(r["status"] == "downloaded" for r in report),
        "mastered": sum(r["status"] == "mastered" for r in report),
        "failed": sum(r["status"] == "failed" for r in report),
        "pending": sum(r["status"] in ("pending", "waiting") for r in report),
        "finished": sum((state.get(t["file"]) or {}).get("v") == MASTER for t in tracks),
        "purged": purged, "audio_mb": round(size / 1048576, 1),
        "minutes": round(sum(m["seconds"] for m in manifest) / 60),
    }
    (ROOT / "fetch-report.json").write_text(json.dumps(
        {"summary": summary, "tracks": report}, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
