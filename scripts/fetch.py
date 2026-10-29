"""
NIGHTDECK library builder. Runs inside GitHub Actions (open internet).

tracks.json  ->  audio/*.mp3 (112 kbps), manifest.json, CREDITS.md, fetch-report.json

- Already-downloaded tracks are skipped, so re-runs are cheap.
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


def transcode(src, dst, t, bitrate):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-map_metadata", "-1",
           "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100",
           "-metadata", f"title={t['title']}", "-metadata", f"artist={t['artist']}",
           "-metadata", f"comment={t['license']} {t['page']}", str(dst)]
    subprocess.run(cmd, check=True)


def main():
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

    tmp = Path(tempfile.mkdtemp())
    for t in tracks:
        dst = ROOT / t["file"]
        entry = {"id": t["id"], "title": t["title"], "artist": t["artist"]}
        if dst.exists() and duration(dst) > 20:
            entry["status"] = "kept"
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
                    transcode(raw, dst, t, bitrate)
                    entry.update(status="downloaded", from_url=url)
                    break
                except Exception as e:
                    entry["tried"].append({"source": src, "error": f"{type(e).__name__}: {e}"[:300]})
                time.sleep(1)
            if entry["status"] == "downloaded":
                entry.pop("tried")
        if dst.exists():
            entry["seconds"] = round(duration(dst))
            manifest.append({
                "id": t["id"], "url": t["file"], "title": t["title"], "artist": t["artist"],
                "library": lib, "license": t["license"], "license_url": t["license_url"],
                "youtube_ok": t["youtube_ok"], "page": t["page"], "seconds": entry["seconds"],
            })
        report.append(entry)
        print(f"[{entry['status']:>10}] {t['artist']} - {t['title']}", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)

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
        "failed": sum(r["status"] == "failed" for r in report),
        "purged": purged, "audio_mb": round(size / 1048576, 1),
        "minutes": round(sum(m["seconds"] for m in manifest) / 60),
    }
    (ROOT / "fetch-report.json").write_text(json.dumps(
        {"summary": summary, "tracks": report}, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
