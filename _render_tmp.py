
import os
src = r"D:\local-multimodal-src\data\library\四次浪潮里的中国坐标_统一版.pptx"
out = r"D:\local-multimodal-src\data\shots\ppt_v3"
import win32com.client as win32
for prog in ("Kwpp.Application", "PowerPoint.Application"):
    try:
        app = win32.Dispatch(prog)
        p = app.Presentations.Open(src, ReadOnly=True, WithWindow=False)
        p.Export(out, "PNG", 1600, 900)
        p.Close()
        app.Quit()
        print("OK via", prog)
        break
    except Exception as e:
        print("fail", prog, repr(e)[:200])
