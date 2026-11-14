# -*- coding: utf-8 -*-
"""诊断：眨眼 ROI 是否过大、姿态曲线是否抖动。只读，不改任何配置。"""
import os, sys, asyncio, subprocess, tempfile
sys.path.insert(0, r"D:\local-multimodal-src")
import numpy as np

BASE = r"D:\local-multimodal-src"
IMG = os.path.join(BASE, "data", "avatar2", "avatar.png")

# 1) 生成一段测试语音
WAV = os.path.join(tempfile.gettempdir(), "_diag_blink.wav")
TXT = "大家好，这是一段用来检查眨眼位置和头部动作是否自然的话。说话的时候头会轻轻转动。"
if not os.path.isfile(WAV):
    mp3 = WAV.replace(".wav", ".mp3")
    try:
        import edge_tts
        async def _go():
            c = edge_tts.Communicate(TXT, "zh-CN-XiaoxiaoNeural", rate="+0%")
            await c.save(mp3)
        asyncio.run(_go())
        from backend import avatar2 as A2
        exe = A2.ffmpeg()
        subprocess.run([exe, "-y", "-loglevel", "error", "-i", mp3,
                        "-ar", "16000", "-ac", "1", WAV], check=True)
    except Exception as e:
        print("TTS 失败:", e)

from backend import avatar_lipsync as AL
from backend import avatar25d as A25

# 2) prep 几何
prep = A25._prep3d(IMG, max_side=1024)
H, W = prep["wh"]
print("=== prep 几何 ===")
print("wh =", (W, H), " fw =", round(prep["fw"], 1), " fh =", round(prep["fh"], 1))
print("eye_gap =", round(prep["eye_gap"], 1))
print("eyes =", [(round(a, 1), round(b, 1)) for a, b in prep["eyes"]])

ew = prep["eye_gap"] * 0.42
eh = ew * 0.62
print("眨眼 ROI: 宽 %d px, 高 %d px  (每只眼)" % (2 * ew, 2 * eh))
for i, (ex, ey) in enumerate(prep["eyes"]):
    print("  眼%d 中心 (%.1f, %.1f) → bbox x[%d,%d] y[%d,%d]"
          % (i, ex, ey, ex - ew, ex + ew, ey - eh, ey + eh))
print("  眼距 %.1f, 单眼 ROI 宽 %.1f = 眼距的 %.0f%%"
      % (prep["eye_gap"], 2 * ew, 2 * ew / prep["eye_gap"] * 100))
print("  脸宽 %.1f, 单眼 ROI 宽占脸宽 %.0f%%"
      % (prep["fw"], 2 * ew / prep["fw"] * 100))

# 3) 姿态曲线抖动
if os.path.isfile(WAV):
    from backend import avatar2 as A2
    sr, y = A2._read_wav(WAV)
    fps = 25
    n = int(len(y) / float(sr) * fps)
    e = AL._energy_curve(y, sr, n, fps)
    poses = A25._pose_curve(n, fps, e)
    arr = np.array(poses, dtype=np.float64)   # n x 7
    names = ["yaw", "pitch", "roll", "tx", "ty", "scl", "blink"]
    print("\n=== 姿态曲线（n=%d 帧, %.2f 秒）===" % (n, n / float(fps)))
    print("%-8s %8s %8s %8s %8s" % ("项", "范围", "最大帧间跳", "p95跳", "跳>阈值帧数"))
    thr = {"yaw": 0.35, "pitch": 0.30, "roll": 0.25,
           "tx": 0.9, "ty": 0.9, "scl": 0.0012, "blink": 0.40}
    for k, nm in enumerate(names):
        v = arr[:, k]
        d = np.abs(np.diff(v)) if len(v) > 1 else np.array([0.0])
        big = int((d > thr[nm]).sum())
        print("%-8s %8.3f %8.3f %8.3f %8d"
              % (nm, v.max() - v.min(), d.max(), np.percentile(d, 95), big))
    # 能量本身
    de = np.abs(np.diff(e))
    print("\n能量 e: 范围 %.2f~%.2f, 帧间跳 max %.3f, p95 %.3f"
          % (e.min(), e.max(), de.max(), np.percentile(de, 95)))
    print("能量跳 >0.15 的帧数:", int((de > 0.15).sum()), "/", len(de))

    # 眨眼帧
    bl = arr[:, 6]
    idx = np.where(bl > 0.02)[0]
    print("\n眨眼帧:", idx.tolist())
    print("眨眼帧的 bl:", [round(float(bl[i]), 2) for i in idx])
    if len(idx):
        print("眨眼期间 ty 跳:", [round(float(abs(arr[i+1, 4]-arr[i, 4])), 2)
                                  for i in idx if i + 1 < n])
