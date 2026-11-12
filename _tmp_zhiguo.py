import sys
import wave

import numpy as np
from scipy.signal import butter, lfilter

sys.path.insert(0, ".")
from backend import music

mid = "gufeng_20261112_231817_5127"
with wave.open(music.audio_path(mid + ".wav"), "rb") as w:
    sr, n = w.getframerate(), w.getnframes()
    a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32).reshape(-1, 2)
mono = a.mean(axis=1)

# 只留 250~1400Hz：滤掉 bass(97~131) 和 kick，保住 flute/pluck 的音区
b, aa = butter(4, [250 / (sr / 2), 1400 / (sr / 2)], btype="band")
x = lfilter(b, aa, mono).astype(np.float32)

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def f0(seg):
    seg = seg - seg.mean()
    if np.sqrt((seg ** 2).mean()) < 200:
        return 0.0
    c = np.correlate(seg, seg, "full")[len(seg) - 1:]
    lo, hi = int(sr / 900), int(sr / 240)
    lag = lo + int(np.argmax(c[lo:hi]))
    return sr / lag if lag else 0.0


def pn(hz):
    if hz <= 0:
        return "--"
    m = 69 + 12 * np.log2(hz / 440.0)
    r = int(round(m))
    return "%s%d" % (NAMES[r % 12], r // 12 - 1)


# 预期：每个音符的起止时间（拍 → 秒，1 拍 = 60/68）
beat = 60.0 / 68.0
notes = [("G4", 1), ("A4", 1), ("C5", 2), ("A4", 1), ("G4", 1), ("E4", 1), ("G4", 2),
         ("G4", 1), ("A4", 1), ("C5", 2), ("A4", 1), ("G4", 1), ("E4", 1), ("D4", 1), ("E4", 2)]
t = 0.0
print("音符 | 预期 | 实测（取音符中段）")
ok = 0
tot = 0
win = int(sr * 0.12)
for name, dur in notes:
    start = t * beat
    mid_t = start + dur * beat * 0.5
    i = int(mid_t * sr)
    if i + win > len(x):
        break
    got = pn(f0(x[i:i + win]))
    tot += 1
    flag = "OK" if got == name else "XX"
    if got == name:
        ok += 1
    print("  %-3s | %-3s | %-3s  %s  (t=%.2fs)" % (name, name, got, flag, mid_t))
    t += dur
print("\n命中 %d/%d" % (ok, tot))
