"""新功能验收断言：自动盯梢台：说一句「盯着XX」就常驻监视

用户反复问同一问题（RSI、重启原因），本质是系统不会主动盯着变化通知他

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import time, threading
    from backend.jobplan import create_task, evaluate_watch_condition
    from backend.scheduler import Scheduler

    state = {'flag': False}
    task = create_task(kind='watch', interval=1, condition="state['flag'] == True")
    sched = Scheduler()
    sched.start()
    time.sleep(0.2)
    assert task['status'] == 'running'
    state['flag'] = True
    time.sleep(2.5)
    assert task['status'] == 'hit', task
    assert len(sched.notifications) > 0
    sched.stop()
    print('watch ok')
