# -*- coding: utf-8 -*-
"""按 EP3763 时间轴 unitOffsets 构建 A/B 双说话轨（18.78s，互补静音，PCM16/48k 单声道）。"""
import array, json, os, struct, wave

ROOT = r"C:\Users\Administrator\Desktop\双人播客项目"
ART = os.path.join(ROOT, "apps", "storage", "artifacts", "audio")
D = os.path.join(ROOT, "链路实测", "_duo")
os.makedirs(D, exist_ok=True)

TOTAL = 901503
RATE = 48000
TRACKS = {
    "a": [("U-T01", 0, "AUD-30f0e05a3a97.wav"), ("U-T03", 459268, "AUD-55dc98f0d426.wav")],
    "b": [("U-T02", 241058, "AUD-829071118d1d.wav"), ("U-T04", 693081, "AUD-438521f6a3ae.wav")],
}

def read_pcm16(fp):
    w = wave.open(fp)
    assert w.getnchannels() == 1 and w.getframerate() == RATE and w.getsampwidth() == 2, (w.getparams(), fp)
    data = array.array("h")
    data.frombytes(w.readframes(w.getnframes()))
    w.close()
    return data

for spk, items in TRACKS.items():
    buf = array.array("h", [0] * TOTAL)
    for uid, off, fn in items:
        d = read_pcm16(os.path.join(ART, fn))
        assert off + len(d) <= TOTAL, (uid, off, len(d))
        buf[off:off + len(d)] = d
        print(f"track_{spk}: {uid} @{off/TOTAL*18.78:.2f}s len={len(d)/48000:.2f}s")
    out = os.path.join(D, f"track_{spk}.wav")
    w = wave.open(out, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    w.writeframes(buf.tobytes())
    w.close()
    print("saved", out, os.path.getsize(out), "bytes")

# 校验：A+B 逐样本相加应等于母轨
m = read_pcm16(os.path.join(ART, "master_R66F6C2FC0AB4_AUD-M-2dcc608a6e9e.wav"))
a = read_pcm16(os.path.join(D, "track_a.wav"))
b = read_pcm16(os.path.join(D, "track_b.wav"))
assert len(m) == len(a) == len(b) == TOTAL
diff = sum(1 for i in range(0, TOTAL, 97) if abs(a[i] + b[i] - m[i]) > 2)
print("sum-check sampled diffs:", diff, "/", TOTAL // 97)
