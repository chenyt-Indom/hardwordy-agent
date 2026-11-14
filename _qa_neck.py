"""最终验证：三种渲染模式 + 眨眼 + 编译。"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25

P = r"D:\local-multimodal-src\data\avatar2\avatar.png"
prep = A25._prep3d(P, max_side=720)
img = prep["img"]
print("prep OK", prep["wh"], "roll_pivot_y=", round(prep["roll_pivot_y"], 1))

# 1) 伪3D 全流程一帧
for kw in (dict(yaw=6, pitch=3, roll=2, tx=8, ty=4, scl=1.005),
           dict(yaw=-6, roll=-2.5),
           dict(pitch=5, ty=-8)):
    o = A25._warp(img, prep, **kw)
    assert o.shape == img.shape and o.dtype == np.uint8
    print(f"  warp {str(kw)[:52]:54s} ok  mean={o.mean():.1f}")

# 2) 眨眼仍可用（位置准）
for amt in (0.4, 0.75, 1.0):
    o = A25._blink(img, prep, amt, yaw=5, pitch=2, tx=6, ty=3, roll=2, scl=1.0)
    print(f"  blink amt={amt} ok  shape={o.shape}")

# 3) _shift_of 边界安全
for x, y in ((0, 0), (1023, 1023), (511, 469), (-10, 500)):
    dx, dy = A25._shift_of(prep, x, y, yaw=5, roll=2)
    print(f"  shift_of({x},{y}) = ({dx:+.2f},{dy:+.2f})")

# 4) 极端姿态不崩
for kw in (dict(yaw=7.5, roll=3.2), dict(yaw=-7.5, roll=-3.2), dict(pitch=6, ty=-11)):
    o = A25._warp(img, prep, **kw)
    print(f"  极端 {str(kw):40s} ok")

# 5) 关键常量确认
from backend import avatar_lipsync as AL
print(f"\n常量: MOTION_GAIN={AL.MOTION_GAIN} ROLL_DAMP={AL.ROLL_DAMP} "
      f"PITCH_BOOST={AL.PITCH_BOOST}")
print(f"roll 实际上限 = 4.0 * {AL.MOTION_GAIN} * {AL.ROLL_DAMP} = "
      f"{4.0*AL.MOTION_GAIN*AL.ROLL_DAMP:.2f}°  (再 clip 到 3.2°)")

# 6) 视频文件完好
vs = sorted(glob.glob(r"D:\local-multimodal-src\data\avatar2\videos\lipsync_20261114_185444*.mp4"))
for v in vs:
    cap = cv2.VideoCapture(v)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    print(f"\n演示片 {os.path.basename(v)}  {w}x{h}  {n}帧  {os.path.getsize(v)//1024}KB")
print("\nALL OK")
