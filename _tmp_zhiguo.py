import json
import sys
import wave

import numpy as np

sys.path.insert(0, ".")
from backend import music

melody = (
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 G4:2 "
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 D4:1 E4:2 "
    "G4:1 A4:1 C5:2 A4:1 G4:1 E4:1 G4:2 "
    "A4:1 C5:1 D5:1 C5:1 A4:1 G4:1 A4:2 "
    "A4:1 C5:1 D5:1 C5:1 A4:1 G4:1 E4:2 "
    "G4:1 E4:1 C5:2 A4:1 A4:1 A4:2 "
    "E4:1 E4:1 G4:1 A4:1 A4:1 A4:3"
)


def stats(mid):
    p = music.audio_path(mid + ".wav")
    with wave.open(p, "rb") as w:
        sr, nch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)
    a = a.reshape(-1, 2)
    L, R = a[:, 0], a[:, 1]
    pl, pr = np.abs(L).max(), np.abs(R).max()
    mono = a.mean(axis=1)
    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    fr = np.fft.rfftfreq(len(mono), 1.0 / sr)
    low = spec[(fr > 20) & (fr < 200)].sum() / max(1e-9, spec.sum())
    return dict(dur=round(n / sr, 2), pl=int(pl), pr=int(pr),
                lr_db=round(float(20 * np.log10(max(1, pr) / max(1, pl))), 2),
                corr=round(float(np.corrcoef(L, R)[0, 1]), 3),
                clip=int((np.abs(a) > 32000).sum()),
                low=round(float(low) * 100, 1),
                rms=int(np.sqrt((mono ** 2).mean())))


for bars, tag in ((15, "短版一遍"), (30, "完整版两遍")):
    r = music.generate(style="gufeng", bars=bars, bpm=68, vocals=False,
                       title="我的祖国（古风器乐·%s）" % tag, notes=melody)
    m = r["music"]
    s = stats(m["id"])
    print("%s  id=%s" % (tag, m["id"]))
    print("   ", json.dumps(s, ensure_ascii=False))
    print("    url:", m["url"])
