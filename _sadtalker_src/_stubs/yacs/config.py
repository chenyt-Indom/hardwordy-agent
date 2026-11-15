# -*- coding: utf-8 -*-
"""yacs.config.CfgNode 的轻量替代（只实现本项目用到的属性访问 + load_cfg）"""
import yaml

class CfgNode(dict):
    def __init__(self, init=None, **kwargs):
        super().__init__()
        if init:
            for k, v in init.items():
                self[k] = CfgNode(v) if isinstance(v, dict) else v
        for k, v in kwargs.items():
            self[k] = v
    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise AttributeError(name)
    def __setattr__(self, name, value):
        self[name] = value
    def freeze(self):
        return self
    def defrost(self):
        return self
    def clone(self):
        import copy
        return copy.deepcopy(self)
    @staticmethod
    def load_cfg(f):
        return load_cfg(f)

def load_cfg(f):
    data = yaml.safe_load(f)
    return CfgNode(data or {})
