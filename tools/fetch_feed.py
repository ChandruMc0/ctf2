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


def probe():
    names = [
        "", "boardroom", "live", "stream", "mic", "drop", "implant", "exfil",
        "covert", "audio", "room", "meeting", "av", "bridge", "feed", "secret",
        "flag", "cam", "camera", "board", "conference", "ultrasonic", "whisper",
    ]
    print("PROBE")
    for name in names:
        url = "https://web-fc7f65a00530436c.web.h7tex.com/" + (f"{name}/index.m3u8" if name else "index.m3u8")
        try:
            data, hdrs = fetch(url + f"?bust={int(time.time())}", timeout=12)
            text = data[:180].decode("utf-8", "replace").replace("\n", " | ")
            print(f"  {name or '/'} {hdrs.get('Content-Type')} {len(data)} {text}")
        except Exception as e:
            print(f"  {name or '/'} ERR {e}")


def decode_delays(symbols):
    """Brute symbol->dibit maps and look for a flag."""
    import itertools
    symbols = [int(s) for s in symbols]
    alphabet = sorted(set(symbols))
    print("SYMBOLS", symbols)
    print("ALPHABET", alphabet)
    (OUT / "symbols.txt").write_text(" ".join(map(str, symbols)) + "\n")
    if len(alphabet) > 8 or len(symbols) < 8:
        print("skip brute", len(alphabet), len(symbols))
        return

    def pack(seq, mapping):
        bits = []
        for s in seq:
            dibit = mapping[s]
            bits.append((dibit >> 1) & 1)
            bits.append(dibit & 1)
        out = bytearray()
        for i in range(0, len(bits) - 7, 8):
            v = 0
            for b in bits[i:i + 8]:
                v = (v << 1) | b
            out.append(v)
        # also lsb-first packing
        out2 = bytearray()
        for i in range(0, len(bits) - 7, 8):
            v = 0
            for k, b in enumerate(bits[i:i + 8]):
                v |= b << k
            out2.append(v)
        return bytes(out), bytes(out2)

    flag_re = re.compile(rb"H7Tex\{[^}\x00]{0,80}\}|h7tex\{[^}]{0,80}\}|flag\{[^}]{0,40}\}")
    hits = []
    base = max(alphabet) + 1
    # map each observed symbol to a dibit 0..3 if alphabet size is 4, else skip dibit brute
    if len(alphabet) == 4:
        for perm in itertools.permutations(range(4)):
            mapping = {alphabet[i]: perm[i] for i in range(4)}
            for rot in range(4):
                seq = [alphabet[(alphabet.index(s) - rot) % 4] if False else s for s in symbols]
                # rotation in symbol space
                seq = [alphabet[(alphabet.index(s) + rot) % 4] for s in symbols]
                for rev in (False, True):
                    s2 = list(reversed(seq)) if rev else seq
                    a, b = pack(s2, mapping)
                    for label, raw in (("msb", a), ("lsb", b)):
                        found = flag_re.findall(raw)
                        if found:
                            hits.append((found, mapping, rot, rev, label, raw[:80]))
    report = ["symbols: " + " ".join(map(str, symbols))]
    if hits:
        print("FLAG_HITS", len(hits))
        for h in hits[:20]:
            print(" HIT", h[0], "map", h[1], "rot", h[2], "rev", h[3], h[4])
            report.append(repr(h))
    else:
        print("no flag in brute")
        report.append("no flag in brute")
    (OUT / "decode.txt").write_text("\n".join(report) + "\n")


def measure_delays(wav_path):
    import wave
    import numpy as np
    w = wave.open(str(wav_path))
    rate = w.getframerate()
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
    print("WAV", rate, pcm.shape, "dur", len(pcm) / rate)
    win = int(rate * 0.02)
    env = np.sqrt(np.convolve(pcm ** 2, np.ones(win) / win, mode="same"))
    hop = rate // 100
    env_ds = env[::hop]
    mx = np.percentile(env_ds, 99)
    mask = env_ds > mx * 0.35
    bursts = []
    inb = False
    start = 0
    for i, v in enumerate(mask):
        if v and not inb:
            start = i
            inb = True
        elif not v and inb:
            bursts.append((start / 100.0, i / 100.0))
            inb = False
    if inb:
        bursts.append((start / 100.0, len(mask) / 100.0))
    bursts = [(a, b) for a, b in bursts if 1.2 < (b - a) < 2.4]
    print("BURSTS", len(bursts))
    if len(bursts) < 3:
        return []
    dur = 1.50
    N = int(dur * rate)
    segs = []
    for a, b in bursts:
        i0 = int(a * rate)
        if i0 + N <= len(pcm):
            segs.append(pcm[i0:i0 + N])
    template = segs[0]
    n = N * 2
    ct = np.fft.rfft(template, n=n)
    symbols = []
    quantum = 160  # samples at 48 kHz = 3.333 ms
    for i, seg in enumerate(segs):
        c = np.fft.irfft(np.fft.rfft(seg, n=n) * np.conj(ct), n=n)
        # search positive lags 0..800 and negative via wrap
        maxlag = 800
        window = np.concatenate([c[-maxlag:], c[:maxlag + 1]])
        lags = np.concatenate([np.arange(-maxlag, 0), np.arange(0, maxlag + 1)])
        k = int(np.argmax(window))
        lag = int(lags[k])
        # fold into 0..639 (4 quanta) relative to template; template lag is 0
        q = int(np.round(lag / quantum))
        symbols.append(q)
        print(f"  burst {i:3} t={bursts[i][0]:7.2f} lag={lag:5} q={q:3} peak={window[k]:.0f}")
    # normalize so minimum quantum is 0
    mn = min(symbols)
    symbols = [s - mn for s in symbols]
    return symbols


def main():
    probe()
    seen = {}
    deadline = time.time() + 420
    while time.time() < deadline and len(seen) < 80:
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
        ["ffmpeg", "-y", "-i", str(concat), "-vn", "-ac", "1", "-ar", "48000", "-acodec", "pcm_s16le", str(wav)],
        check=False,
    )
    symbols = measure_delays(wav)
    (OUT / "symbols.txt").write_text(" ".join(map(str, symbols)) + "\n")
    print("SYMBOLS", symbols)
    decode_delays(symbols)
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
