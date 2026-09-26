#!/usr/bin/env python3
"""Fetch the boardroom HLS audio feed and look for an inaudible exfil channel."""
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://web-fc7f65a00530436c.web.h7tex.com/boardroom"
OUT = Path("analysis-out")
OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (compatible; feed-audit/1.0)"


def fetch(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers


def playlist_urls():
    bust = int(time.time() * 1000)
    url = f"{BASE}/main_stream.m3u8?bust={bust}"
    body, hdrs = fetch(url)
    text = body.decode("utf-8", "replace")
    (OUT / "playlist.m3u8").write_text(text)
    print("PLAYLIST", text)
    print("PLAYLIST_HEADERS", dict(hdrs))
    segs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # drop cache-buster query so the segment URL is the canonical one
        path = line.split("?", 1)[0]
        segs.append(urllib.parse.urljoin(url, path))
    return segs


def main():
    seen = {}
    deadline = time.time() + 50
    while time.time() < deadline and len(seen) < 12:
        try:
            for url in playlist_urls():
                name = url.rsplit("/", 1)[-1]
                if name in seen:
                    continue
                data, hdrs = fetch(url)
                seen[name] = data
                print(f"SEG {name} bytes={len(data)} ctype={hdrs.get('Content-Type')}")
                (OUT / name).write_bytes(data)
        except Exception as e:
            print("FETCH_ERR", type(e).__name__, e)
        if len(seen) >= 12:
            break
        time.sleep(4)

    if not seen:
        raise SystemExit("no segments")

    def seq(name):
        m = re.search(r"seg(\d+)", name)
        return int(m.group(1)) if m else 0

    names = sorted(seen, key=seq)
    concat = OUT / "feed.ts"
    with concat.open("wb") as f:
        for n in names:
            f.write(seen[n])
    print("CONCAT", concat, concat.stat().st_size, "segs", names)

    subprocess.run(["ffprobe", "-hide_banner", "-show_streams", "-show_format", str(concat)], check=False)
    wav = OUT / "feed.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(concat), "-vn", "-acodec", "pcm_s16le", str(wav)],
        check=False,
    )
    # full spectrogram and a high-band crop
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav),
            "-lavfi", "showspectrumpic=s=1600x900:legend=1:mode=combined",
            str(OUT / "spectrogram.png"),
        ],
        check=False,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav),
            "-lavfi", "highpass=f=14000,showspectrumpic=s=1600x600:legend=1:mode=combined",
            str(OUT / "spectrogram_high.png"),
        ],
        check=False,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav),
            "-lavfi", "highpass=f=16000,lowpass=f=23000,showspectrumpic=s=1800x500:legend=1:scale=lin",
            str(OUT / "spectrogram_ultra.png"),
        ],
        check=False,
    )
    # slow the ultrasonic band into the audible range and re-image it
    slow = OUT / "ultra_slow.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav),
            "-af", "highpass=f=15000,asetrate=4800,aresample=16000,volume=8",
            str(slow),
        ],
        check=False,
    )
    if slow.exists() and slow.stat().st_size > 44:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(slow),
                "-lavfi", "showspectrumpic=s=1800x700:legend=1",
                str(OUT / "spectrogram_slow.png"),
            ],
            check=False,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(slow), "-ac", "1", "-ar", "8000", str(OUT / "ultra_slow_8k.wav")],
            check=False,
        )

    raw = concat.read_bytes()
    strings = re.findall(rb"[\x20-\x7e]{6,}", raw)
    interesting = [s for s in strings if re.search(rb"H7|flag|tex|secret|key|pass|http|m3u", s, re.I)]
    print("STRINGS_INTERESTING", interesting[:80])
    print("STRING_COUNT", len(strings))
    (OUT / "strings.txt").write_text("\n".join(s.decode("ascii", "replace") for s in strings[:400]))
    hits = []
    for pat in (b"H7Tex", b"h7tex", b"H7TEX", b"flag{", b"FLAG{"):
        i = raw.find(pat)
        if i >= 0:
            hits.append(raw[max(0, i - 20): i + 120])
    print("RAW_HITS", hits)
    if wav.exists():
        wraw = wav.read_bytes()
        for pat in (b"H7Tex", b"h7tex", b"flag{"):
            i = wraw.find(pat)
            if i >= 0:
                print("WAV_HIT", wraw[i:i+80])
    print("DONE")
    for p in sorted(OUT.iterdir()):
        print("OUT", p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
