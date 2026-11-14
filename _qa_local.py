import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT); sys.path.insert(0, ROOT)
from backend import main as M
try:
    ms = M._installed_models()
    print("本地模型：", ms if ms else "（无）")
except Exception as e:
    print("查询失败：", e)
cfg = M.config.load_config()
print("默认模型：", cfg.get("default_model"))
print("Ollama 可达：", getattr(M.client, "alive", lambda: "?")() if hasattr(M.client, "alive") else "未知")
