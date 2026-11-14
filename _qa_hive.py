# -*- coding: utf-8 -*-
"""验证群聊改造：编译 + 长文转附件 + 附件链路完整性。"""
import os
import sys
import py_compile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

ok = 0
fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s %s" % (name, extra))
    else:
        fail += 1
        print("  FAIL  %s %s" % (name, extra))


print("=" * 60)
print("1) 编译")
for f in ("backend/main.py", "backend/hive.py", "backend/hive_client.py",
          "backend/hive_mesh.py"):
    try:
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
        chk(f, True)
    except Exception as e:
        chk(f, False, str(e))

print("=" * 60)
print("2) 源码检查：max_tokens 是否还有硬编码")
src = open(os.path.join(ROOT, "backend/main.py"), encoding="utf-8").read()
import re
hard = []
for ln in src.splitlines():
    s = ln.strip()
    if s.startswith("#"):
        continue                      # 注释里提到旧值不算硬编码
    if re.search(r'"max_tokens":\s*2400|max_tokens=2400', ln):
        hard.append(ln)
chk("无 max_tokens=2400 硬编码", len(hard) == 0, "残留 %d 处" % len(hard))
chk("_hive_mt 可配置读取", "hive_max_tokens" in src)
chk("_hive_mt 夹紧 512~32000", "max(512, min(32000, _hive_mt))" in src)
chk("_hive_mt 缺省跟随单人 max_tokens", 'cfg.get("max_tokens") or 8192' in src)
csrc = open(os.path.join(ROOT, "backend/config.py"), encoding="utf-8").read()
chk("config 有 hive_max_tokens 键", '"hive_max_tokens"' in csrc)
chk("config 默认 0=跟随单人", '"hive_max_tokens": 0' in csrc)
chk("config 单人 max_tokens 足够大",
    '"max_tokens": 8192' in csrc)

print("=" * 60)
print("3) 提示词：长文不再自我压缩")
hsrc = open(os.path.join(ROOT, "backend/hive.py"), encoding="utf-8").read()
chk("明确要求完整写出", "必须完整写出来" in hsrc)
chk("说明长文会自动转附件", "另存成附件" in hsrc)
chk("代码仍保留 60 行规则", "超过 60 行" in hsrc)

print("=" * 60)
print("4) 长文转附件函数")
chk("HIVE_LONG_CHARS 定义", "HIVE_LONG_CHARS = 3000" in src)
chk("_hive_attach_long_text 定义", "def _hive_attach_long_text" in src)
chk("调用处已接入", "extra = _hive_attach_long_text(gid, final, extra)" in src)
chk("附件广播已接", "_long_files = list((extra or {}).get(\"files\") or [])" in src)

print("=" * 60)
print("5) 附件链路（图片/文件自动接收）")
c = open(os.path.join(ROOT, "backend/hive_client.py"), encoding="utf-8").read()
chk("发送：512KB 分片", "CH = 512 * 1024" in c)
chk("发送：广播 T_FILE", "MESH.T_FILE" in c)
chk("接收：攒齐才落盘", "ready = len(st[\"parts\"]) >= st[\"total\"]" in c)
chk("接收：落盘 store_incoming", "H.store_incoming(gid, meta, data)" in c)
chk("接收：回调 on_file", "self.on_file(gid, rec)" in c)
m = open(os.path.join(ROOT, "backend/main.py"), encoding="utf-8").read()
chk("main：on_file 已注册", "hive_client.client().on_file = _hive_on_file" in m)
chk("main：推前端 file 帧", '"type": "file"' in m)
chk("main：无人在线进补发队列", "_queue_pending_file" in m)
chk("main：上线自动补发", "_flush_pending_files" in m)

print("=" * 60)
print("6) 导入 main（真导入一遍，语法/依赖错误会立刻暴露）")
try:
    import backend.main as M  # noqa: F401
    chk("import backend.main", True)
    routes = [r for r in M.app.routes if getattr(r, "path", "").startswith("/api/hive")]
    chk("hive 路由数 > 30", len(routes) > 30, "实际 %d" % len(routes))
    need = ["/api/hive/{gid}/send", "/api/hive/{gid}/upload",
            "/api/hive/{gid}/files/{fid}"]
    paths = {getattr(r, "path", "") for r in M.app.routes}
    for n in need:
        chk("路由存在 " + n, n in paths)
except Exception as e:
    import traceback
    traceback.print_exc()
    chk("import backend.main", False, str(e))

print("=" * 60)
print("结果：PASS %d / FAIL %d" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
