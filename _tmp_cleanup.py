# -*- coding: utf-8 -*-
import os
try:
    os.remove("_tmp_cleanup.py")
    print("removed self")
except Exception as e:  # noqa: BLE001
    print("err", e)
