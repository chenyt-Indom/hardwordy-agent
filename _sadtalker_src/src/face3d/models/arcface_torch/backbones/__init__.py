# 仅推理用：RecogNetWrapper 只在训练时构建，这里给个占位实现
def get_model(name, **kwargs):
    raise NotImplementedError("arcface 只在训练时使用，推理不需要")
