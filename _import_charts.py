# -*- coding: utf-8 -*-
"""把 charts/ 里的 3D 图表 PNG 通过程序自带的 image_library.save_image 入库。
可重复执行：同名已存在则跳过，不会重复入库。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import image_library as lib  # noqa: E402

CH = os.path.join(ROOT, "charts")
SRC_NOTE = "本地图表文件 · 票房数字为示意模型值，非真实统计"

FILES = [
    ("2026中外票房_3D图表.png", "2026中外票房_3D图表（示意数据）"),
    ("2026中外票房_真3D.png", "2026中外票房_真3D（示意数据）"),
    ("3D图表_修复后.png", "3D图表_修复后（示意数据）"),
    ("3D饼图_修复前后对比.png", "3D饼图_修复前后对比（示意数据）"),
]


def main() -> int:
    print("图库目录:", lib.DIR)
    print("索引文件:", lib.INDEX)
    before = len(lib.list_images())
    print("入库前数量:", before)

    existing = {m.get("name") for m in lib.list_images()}
    added, skipped, missing = 0, 0, 0

    for fn, nm in FILES:
        p = os.path.join(CH, fn)
        if not os.path.isfile(p):
            print(f"  [缺失] {fn}")
            missing += 1
            continue
        if nm in existing:
            print(f"  [跳过] 已存在同名：{nm}")
            skipped += 1
            continue
        meta = lib.save_image(p, name=nm, source=SRC_NOTE, origin="upload")
        if meta.get("ok") is False:
            print(f"  [失败] {fn} -> {meta}")
            continue
        print(f"  [入库] {fn} -> id={meta.get('id')} "
              f"{meta.get('w')}x{meta.get('h')} {meta.get('size')}B")
        added += 1

    items = lib.list_images()
    print(f"\n入库后数量: {len(items)}（新增 {added} / 跳过 {skipped} / 缺失 {missing}）")

    # 回读校验：确认文件真的在磁盘上、尺寸元数据对得上
    print("\n回读校验:")
    for m in items:
        if m.get("name") not in {n for _, n in FILES}:
            continue
        p = lib.get_path(m["id"])
        ok = bool(p) and os.path.isfile(p)
        print(f"  {'OK ' if ok else 'BAD'} {m['name']} | id={m['id']} | "
              f"{m.get('w')}x{m.get('h')} | {os.path.basename(p) if p else '文件缺失'}")
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
