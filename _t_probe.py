import sys, os
print("cwd:", os.getcwd(), flush=True)
from backend import avatar2 as A2
print("import A2 OK", flush=True)
print("img:", A2._img_path(), flush=True)
print("MAX_SIDE:", A2.MAX_SIDE, flush=True)
