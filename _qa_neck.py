"""定位整帧变糊的来源：编码参数 / warp / 缩放。"""
import sys, os, glob, subprocess, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, cv2
from backend import avatar25d as A25
from backend import avatar_lipsync as AL

vs = sorted(glob.glob(r"D:\local-multimodal-src\data\avatar2\videos\lipsync_20261114_185444*.mp4"))
v = vs[0]
print("视频:", os.path.basename(v), os.path.getsize(v)//1024, "KB")
# ffprobe
try:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                          "-show_streams", "-show_format", v],
                         capture_output=True, text=True, timeout=30)
    j = json.loads(out.stdout)
    for s in j.get("streams", []):
        if s.get("codec_type") == "video":
            print(f"  codec={s.get('codec_name')} {s.get('width')}x{s.get('height')} "
                  f"bit_rate={int(s.get('bit_rate',0))//1000}kbps fps={s.get('r_frame_rate')} "
                  f"pix_fmt={s.get('pix_fmt')}")
    print("  format bit_rate=", int(j.get("format", {}).get("bit_rate", 0))//1000, "kbps")
except Exception as e:
    print("  ffprobe 失败:", e)

def lapv(a):
    return cv2.Laplacian(a.astype(np.float32), cv2.CV_32F).var()

prep = A25._prep3d(r"D:\local-multimodal-src\data\avatar2\avatar.png", max_side=1024)
img = prep["img"]
g0 = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
print(f"\n原图 {img.shape[1]}x{img.shape[0]} 清晰度: {lapv(g0):.2f}")

cap = cv2.VideoCapture(v)
ok, f0 = cap.read(); cap.release()
gv = cv2.cvtColor(f0, cv2.COLOR_BGR2GRAY)
print(f"成片首帧 {gv.shape[1]}x{gv.shape[0]} 清晰度: {lapv(gv):.2f}  比值 {lapv(gv)/lapv(g0):.2f}")

# 不经过任何处理：原图直接 H264 编码再解回，看纯编码损失
tmp = r"D:\local-multimodal-src\_qa_out\_codec_test.mp4"
os.makedirs(os.path.dirname(tmp), exist_ok=True)
h, w = g0.shape
vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), 25, (w, h))
for _ in range(5):
    vw.write(cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
vw.release()
cap = cv2.VideoCapture(tmp); ok, fc = cap.read(); cap.release()
gc = cv2.cvtColor(fc, cv2.COLOR_BGR2GRAY)
print(f"纯编码损失（原图→mp4v→解回）清晰度: {lapv(gc):.2f}  比值 {lapv(gc)/lapv(g0):.2f}")

# warp 一次（静止参数，应等价于恒等）的损失
gw = A25._warp(img, prep, yaw=0.0)
print(f"warp(恒等) 清晰度: {lapv(cv2.cvtColor(gw, cv2.COLOR_RGB2GRAY)):.2f}")

# 实际一帧 warp 后（用视频里的姿态近似）
for yaw in (2.0, 5.0):
    g1 = A25._warp(img, prep, yaw=yaw)
    print(f"warp(yaw={yaw}) 清晰度: {lapv(cv2.cvtColor(g1, cv2.COLOR_RGB2GRAY)):.2f}")

# 看 render 里用的编码器参数
import inspect
src = inspect.getsource(AL.render)
for ln in src.splitlines():
    if any(k in ln for k in ("ffmpeg", "crf", "preset", "libx264", "vcodec", "VideoWriter")):
        print("  render>", ln.strip())
