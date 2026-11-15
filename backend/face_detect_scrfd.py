"""SCRFD 人脸检测 + 5 关键点（纯 onnxruntime，无 insightface 依赖）

输出与 insightface SCRFD 一致：bbox(x1,y1,x2,y2,score) + kps(5,2)
"""
import numpy as np
import cv2
import onnxruntime as ort
import logging
logger = logging.getLogger(__name__)


def _distance2bbox(points, distance):
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points, distance):
    preds = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, i % 2] + distance[:, i]
        py = points[:, i % 2 + 1] + distance[:, i + 1]
        preds.append(px)
        preds.append(py)
    return np.stack(preds, axis=-1)


def _nms(dets, thresh):
    if len(dets) == 0:
        return []
    x1, y1, x2, y2, sc = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = sc.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= thresh)[0]
        order = order[inds + 1]
    return keep


class SCRFD:
    def __init__(self, onnx_path, input_size=(640, 640), num_anchors=2):
        so = ort.SessionOptions()
        so.intra_op_num_threads = 4
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(onnx_path, so, providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        self.input_size = input_size
        self.num_anchors = num_anchors
        self.strides = [8, 16, 32]

    def detect(self, img_bgr, thresh=0.5, nms_thresh=0.4, max_num=0):
        h0, w0 = img_bgr.shape[:2]
        iw, ih = self.input_size
        scale = min(iw / w0, ih / h0)
        nw, nh = int(round(w0 * scale)), int(round(h0 * scale))
        resized = cv2.resize(img_bgr, (nw, nh))
        canvas = np.zeros((ih, iw, 3), dtype=np.uint8)
        canvas[:nh, :nw] = resized
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (iw, ih),
                                     (127.5, 127.5, 127.5), swapRB=True)
        outs = self.sess.run(None, {self.input_name: blob})

        scores_list, bboxes_list, kpss_list = [], [], []
        for idx, stride in enumerate(self.strides):
            scores = outs[idx]
            bbox_preds = outs[idx + len(self.strides)] * stride
            kps_preds = outs[idx + 2 * len(self.strides)] * stride
            height = ih // stride
            width = iw // stride
            ac = np.stack(np.mgrid[:height, :width][::-1], axis=-1).astype(np.float32)
            ac = (ac * stride).reshape((-1, 2))
            if self.num_anchors > 1:
                ac = np.stack([ac] * self.num_anchors, axis=1).reshape((-1, 2))
            pos = np.where(scores[:, 0] >= thresh)[0]
            if len(pos) == 0:
                continue
            scores_list.append(scores[pos, 0])
            bboxes_list.append(_distance2bbox(ac[pos], bbox_preds[pos]))
            kpss_list.append(_distance2kps(ac[pos], kps_preds[pos]))

        if not scores_list:
            return np.zeros((0, 5), np.float32), np.zeros((0, 5, 2), np.float32)

        scores = np.concatenate(scores_list).ravel()
        bboxes = np.vstack(bboxes_list) / scale
        kpss = np.vstack(kpss_list).reshape(-1, 5, 2) / scale
        dets = np.hstack([bboxes, scores[:, None]]).astype(np.float32)
        keep = _nms(dets, nms_thresh)
        dets = dets[keep]
        kpss = kpss[keep]
        if max_num > 0 and len(dets) > max_num:
            dets = dets[:max_num]
            kpss = kpss[:max_num]
        # 裁剪到图像内
        dets[:, 0] = np.clip(dets[:, 0], 0, w0 - 1)
        dets[:, 1] = np.clip(dets[:, 1], 0, h0 - 1)
        dets[:, 2] = np.clip(dets[:, 2], 0, w0 - 1)
        dets[:, 3] = np.clip(dets[:, 3], 0, h0 - 1)
        return dets, kpss
