
import sys
sys.path.insert(0, r"D:\local-multimodal-src")
from backend.main import app
routes = [r.path for r in app.routes if hasattr(r,"path")]
a3 = [p for p in routes if "avatar3d" in p]
print("avatar3d 路由:", a3)
print("总路由:", len(routes))
