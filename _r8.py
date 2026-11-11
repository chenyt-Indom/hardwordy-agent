
import win32com.client as win32
app = win32.Dispatch("Kwpp.Application")
p = app.Presentations.Open(r"D:\local-multimodal-src\data\library\四次浪潮里的中国坐标_统一版.pptx", ReadOnly=True, WithWindow=False)
p.Export(r"D:\local-multimodal-src\data\shots\ppt_v3h", "PNG", 1600, 900)
p.Close(); app.Quit(); print("ok")
