import sys
import wave

import numpy as np

sys.path.insert(0, ".")
from backend import music

mid = "gufeng_20261112_231715_0536"
p = music.audio_path(mid + ".wav")
print("wav:", p)
with wave.open(p, "rb") as w:
    sr = w.getframerate()
    nch = w.getnchannels()
    n = w.getnframes()
    raw = w.readframes(n)
a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
print("sr=%d ch=%d frames=%d dur=%.2fs" % (sr, nch, n, n / sr))
if nch == 2:
    a = a.reshape(-1, 2)
    L, R = a[:, 0], a[:, 1]
    print("peak L=%.0f R=%.0f (int16 max 32767)" % (np.abs(L).max(), np.abs(R).max()))
    print("clip>32000:", int((np.abs(a) > 32000).sum()))
    corr = float(np.corrcoef(L, R)[0, 1])
    print("L/R corr=%.3f" % corr)
    mono = a.mean(axis=1)
else:
    mono = a
    print("peak=%.0f" % np.abs(mono).max())

spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
fr = np.fft.rfftfreq(len(mono), 1.0 / sr)
low = spec[(fr > 20) & (fr < 200)].sum() / max(1e-9, spec.sum())
print("低频(20-200Hz)占比: %.1f%%" % (low * 100))
print("RMS: %.0f" % np.sqrt((mono ** 2).mean()))

# 基频轨迹（自相关）
def f0(seg, sr):
    seg = seg - seg.mean()
    if np.sqrt((seg ** 2).mean()) < 300:
        return 0
    c = np.correlate(seg, seg, "full")[len(seg) - 1:]
    lo, hi = int(sr / 800), int(sr / 80)
    if hi >= len(c):
        return 0
    lag = lo + int(np.argmax(c[lo:hi]))
    return sr / lag if lag else 0


NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
def pn(hz):
    if hz <= 0:
        return "--"
    m = 69 + 12 * np.log2(hz / 440.0)
    r = int(round(m))
    return "%s%d(%d)" % (NAMES[r % 12], r // 12 - 1, int(round(hz)))

win = int(sr * 0.09)
print("\n前 8 秒基频轨迹（每 0.45 秒取样）:")
expect = [(0.0, "G4"), (0.9, "A4"), (1.8, "C5"), (3.6, "A4"), (4.5, "G4"), (5.3, "E4"), (6.2, "G4")]
for t in np.arange(0.1, 8.0, 0.45):
    i = int(t * sr)
    seg = mono[i:i + win]
    if len(seg) < win:
        break
    print("  t=%.2fs  %s" % (t, pn(f0(seg, sr))))
print("\n预期（按输入旋律）:", expect)
