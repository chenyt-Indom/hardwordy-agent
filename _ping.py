import time
t = time.time()
print("hello", flush=True)
import numpy as np, cv2
print("import %.1fs" % (time.time()-t), flush=True)
print("numpy", np.__version__, "cv2", cv2.__version__, flush=True)
