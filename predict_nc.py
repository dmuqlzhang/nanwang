#coding=utf-8


import os
import cv2
import math
import time
import torch
import torchvision
import torch.nn as nn
import numpy as np
from numpy import random
import threading
import sys
import importlib
import json
from tqdm import tqdm
from pascal_voc_writer import Writer
import argparse
import datetime


global_loading_lock = threading.Lock()

def attempt_load(model_path, map_location=None):
    with global_loading_lock:
        default_path_length = len(sys.path)
        default_modules_length = len(sys.modules)
        #sys.path.append(module_dir)
        
        model: torch.nn.Module = torch.load(model_path, map_location=map_location)['model']
        sys.path = sys.path[:default_path_length]
        for module_name in list(sys.modules.keys())[default_modules_length:]:
            del sys.modules[module_name]
        importlib.invalidate_caches()
    for m in model.modules():
        if type(m) in [nn.Hardswish, nn.LeakyReLU, nn.ReLU, nn.ReLU6]:
            m.inplace = True  # pytorch 1.7.0 compatibility
        elif type(m).__name__ == "Conv":
            m._non_persistent_buffers_set = set()  # pytorch 1.6.0 compatibility
    return model


def xywh2xyxy(x):
    # Convert nx4 boxes from [x, y, w, h] to [x1, y1, x2, y2] where xy1=top-left, xy2=bottom-right
    y = x.clone() if isinstance(x, torch.Tensor) else np.copy(x)
    y[:, 0] = x[:, 0] - x[:, 2] / 2  # top left x
    y[:, 1] = x[:, 1] - x[:, 3] / 2  # top left y
    y[:, 2] = x[:, 0] + x[:, 2] / 2  # bottom right x
    y[:, 3] = x[:, 1] + x[:, 3] / 2  # bottom right y
    return y


def letterbox(img, new_shape=(640, 640), color=(114, 114, 114), auto=True, scaleFill=False, scaleup=True):
    # Resize image to a 32-pixel-multiple rectangle https://github.com/ultralytics/yolov3/issues/232
    shape = img.shape[:2]  # current shape [height, width]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    # Scale ratio (new / old)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    if not scaleup:  # only scale down, do not scale up (for better test mAP)
        r = min(r, 1.0)

    # Compute padding
    ratio = r, r  # width, height ratios
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]  # wh padding
    if auto:  # minimum rectangle
        dw, dh = np.mod(dw, 64), np.mod(dh, 64)  # wh padding
    elif scaleFill:  # stretch
        dw, dh = 0.0, 0.0
        new_unpad = (new_shape[1], new_shape[0])
        ratio = new_shape[1] / shape[1], new_shape[0] / shape[0]  # width, height ratios

    dw /= 2  # divide padding into 2 sides
    dh /= 2

    if shape[::-1] != new_unpad:  # resize
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)  # add border
    return img, ratio, (dw, dh)


def scale_coords(img1_shape, coords, img0_shape, ratio_pad=None):
    # Rescale coords (xyxy) from img1_shape to img0_shape
    if ratio_pad is None:  # calculate from img0_shape
        gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])  # gain  = old / new
        pad = (img1_shape[1] - img0_shape[1] * gain) / 2, (img1_shape[0] - img0_shape[0] * gain) / 2  # wh padding
    else:
        gain = ratio_pad[0][0]
        pad = ratio_pad[1]

    coords[:, [0, 2]] -= pad[0]  # x padding
    coords[:, [1, 3]] -= pad[1]  # y padding
    coords[:, :4] /= gain
    clip_coords(coords, img0_shape)
    return coords


def clip_coords(boxes, img_shape):
    # Clip bounding xyxy bounding boxes to image shape (height, width)
    boxes[:, 0].clamp_(0, img_shape[1])  # x1
    boxes[:, 1].clamp_(0, img_shape[0])  # y1
    boxes[:, 2].clamp_(0, img_shape[1])  # x2
    boxes[:, 3].clamp_(0, img_shape[0])  # y2


def box_iou(box1, box2):
    # https://github.com/pytorch/vision/blob/master/torchvision/ops/boxes.py
    """
    Return intersection-over-union (Jaccard index) of boxes.
    Both sets of boxes are expected to be in (x1, y1, x2, y2) format.
    Arguments:
        box1 (Tensor[N, 4])
        box2 (Tensor[M, 4])
    Returns:
        iou (Tensor[N, M]): the NxM matrix containing the pairwise
            IoU values for every element in boxes1 and boxes2
    """

    def box_area(box):
        # box = 4xn
        return (box[2] - box[0]) * (box[3] - box[1])

    area1 = box_area(box1.T)
    area2 = box_area(box2.T)

    # inter(N,M) = (rb(N,M,2) - lt(N,M,2)).clamp(0).prod(2)
    inter = (torch.min(box1[:, None, 2:], box2[:, 2:]) - torch.max(box1[:, None, :2], box2[:, :2])).clamp(0).prod(2)
    return inter / (area1[:, None] + area2 - inter)  # iou = inter / (area1 + area2 - inter)


def non_max_suppression(prediction, conf_thres=0.25, iou_thres=0.2, classes=None, agnostic=False, labels=()):
    """Performs Non-Maximum Suppression (NMS) on inference results

    Returns:
         detections with shape: nx6 (x1, y1, x2, y2, conf, cls)
    """
    #print(prediction.shape)
    nc = prediction.shape[2] - 5  # number of classes
    xc = prediction[..., 4] > conf_thres  # candidates

    # Settings
    min_wh, max_wh = 2, 4096  # (pixels) minimum and maximum box width and height
    max_det = 300  # maximum number of detections per image
    max_nms = 30000  # maximum number of boxes into torchvision.ops.nms()
    time_limit = 10.0  # seconds to quit after
    redundant = True  # require redundant detections
    multi_label = nc > 1  # multiple labels per box (adds 0.5ms/img)
    merge = False  # use merge-NMS

    t = time.time()
    output = [torch.zeros((0, 6), device=prediction.device)] * prediction.shape[0]
    for xi, x in enumerate(prediction):  # image index, image inference
        # Apply constraints
        # x[((x[..., 2:4] < min_wh) | (x[..., 2:4] > max_wh)).any(1), 4] = 0  # width-height
        x = x[xc[xi]]  # confidence

        # Cat apriori labels if autolabelling
        if labels and len(labels[xi]):
            l = labels[xi]
            v = torch.zeros((len(l), nc + 5), device=x.device)
            v[:, :4] = l[:, 1:5]  # box
            v[:, 4] = 1.0  # conf
            v[range(len(l)), l[:, 0].long() + 5] = 1.0  # cls
            x = torch.cat((x, v), 0)

        # If none remain process next image
        if not x.shape[0]:
            continue

        # Compute conf
        x[:, 5:] *= x[:, 4:5]  # conf = obj_conf * cls_conf

        # Box (center x, center y, width, height) to (x1, y1, x2, y2)
        box = xywh2xyxy(x[:, :4])

        # Detections matrix nx6 (xyxy, conf, cls)
        if multi_label:
            i, j = (x[:, 5:] > conf_thres).nonzero(as_tuple=False).T
            x = torch.cat((box[i], x[i, j + 5, None], j[:, None].float()), 1)
        else:  # best class only
            conf, j = x[:, 5:].max(1, keepdim=True)
            x = torch.cat((box, conf, j.float()), 1)[conf.view(-1) > conf_thres]

        # Filter by class
        if classes is not None:
            x = x[(x[:, 5:6] == torch.tensor(classes, device=x.device)).any(1)]

        # Apply finite constraint
        # if not torch.isfinite(x).all():
        #     x = x[torch.isfinite(x).all(1)]

        # Check shape
        n = x.shape[0]  # number of boxes
        if not n:  # no boxes
            continue
        elif n > max_nms:  # excess boxes
            x = x[x[:, 4].argsort(descending=True)[:max_nms]]  # sort by confidence

        # Batched NMS
        c = x[:, 5:6] * (0 if agnostic else max_wh)  # classes
        boxes, scores = x[:, :4] + c, x[:, 4]  # boxes (offset by class), scores
        i = torchvision.ops.nms(boxes, scores, iou_thres)  # NMS
        if i.shape[0] > max_det:  # limit detections
            i = i[:max_det]
        if merge and (1 < n < 3E3):  # Merge NMS (boxes merged using weighted mean)
            # update boxes as boxes(i,4) = weights(i,n) * boxes(n,4)
            iou = box_iou(boxes[i], boxes) > iou_thres  # iou matrix
            weights = iou * scores[None]  # box weights
            x[i, :4] = torch.mm(weights, x[:, :4]).float() / weights.sum(1, keepdim=True)  # merged boxes
            if redundant:
                i = i[iou.sum(1) > 1]  # require redundancy

        output[xi] = x[i]
        if (time.time() - t) > time_limit:
            print(f'WARNING: NMS time limit {time_limit}s exceeded')
            break  # time limit exceeded

    return output


def make_divisible(x, divisor):
    # Returns x evenly divisible by divisor
    return math.ceil(x / divisor) * divisor


def check_img_size(img_size, s=32):
    # Verify img_size is a multiple of stride s
    new_size = make_divisible(img_size, int(s))  # ceil gs-multiple
    if new_size != img_size:
        print('WARNING: --img-size %g must be multiple of max stride %g, updating to %g' % (img_size, s, new_size))
    return new_size


def plot_one_box(x, img, color=None, label=None, line_thickness=None):
    # Plots one bounding box on image img
    tl = line_thickness or round(0.002 * (img.shape[0] + img.shape[1]) / 2) + 1  # line/font thickness
    color = color or [random.randint(0, 255) for _ in range(3)]
    c1, c2 = (int(x[0]), int(x[1])), (int(x[2]), int(x[3]))
    cv2.rectangle(img, c1, c2, color, thickness=tl, lineType=cv2.LINE_AA)
    if label:
        tf = max(tl - 1, 1)  # font thickness
        t_size = cv2.getTextSize(label, 0, fontScale=tl / 3, thickness=tf)[0]
        c2 = c1[0] + t_size[0], c1[1] - t_size[1] - 3
        cv2.rectangle(img, c1, c2, color, -1, cv2.LINE_AA)  # filled
        cv2.putText(img, label, (c1[0], c1[1] - 2), 0, tl / 3, [225, 255, 255], thickness=tf, lineType=cv2.LINE_AA)


def select_device(device='', batch_size=None):
    # device = 'cpu' or '0' or '0,1,2,3'
    s = f'Using torch {torch.__version__} '  # string
    cpu = device.lower() == 'cpu'
    if cpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'  # force torch.cuda.is_available() = False
    elif device:  # non-cpu device requested
        os.environ['CUDA_VISIBLE_DEVICES'] = device  # set environment variable
        assert torch.cuda.is_available(), f'CUDA unavailable, invalid device {device} requested'  # check availability

    cuda = torch.cuda.is_available() and not cpu
    if cuda:
        n = torch.cuda.device_count()
        if n > 1 and batch_size:  # check that batch_size is compatible with device_count
            assert batch_size % n == 0, f'batch-size {batch_size} not multiple of GPU count {n}'
        space = ' ' * len(s)
        for i, d in enumerate(device.split(',') if device else range(n)):
            p = torch.cuda.get_device_properties(i)
            s += f"{'' if i == 0 else space}CUDA:{d} ({p.name}, {p.total_memory / 1024 ** 2}MB)\n"  # bytes to MB
    else:
        s += 'CPU'
    return torch.device('cuda:0' if cuda else 'cpu')


def auto_resize(img, max_w, max_h):
    h, w = img.shape[:2]
    scale = min(max_w / w, max_h / h, 1)
    new_size = tuple(map(int, np.array(img.shape[:2][::-1]) * scale))
    return cv2.resize(img, new_size), scale


def xyxy2xywh(x):
    # Convert nx4 boxes from [x1, y1, x2, y2] to [x, y, w, h] where xy1=top-left, xy2=bottom-right
    y = x.clone() if isinstance(x, torch.Tensor) else np.copy(x)
    y[:, 0] = (x[:, 0] + x[:, 2]) / 2  # x center
    y[:, 1] = (x[:, 1] + x[:, 3]) / 2  # y center
    y[:, 2] = x[:, 2] - x[:, 0]  # width
    y[:, 3] = x[:, 3] - x[:, 1]  # height
    return y


def processImg(img_mat, new_shape):
    img = letterbox(img_mat, new_shape=new_shape)[0]
    img = img[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB, to 3x416x416
    return np.ascontiguousarray(img)


class Detector:

    def __init__(self, model_path, img_size=416, conf_thres=0.25, iou_thres=0.2, device='',
                 agnostic_nms=False, draw_box=True, augment=False, profile=False):
        self.device = select_device(device)
        device = self.device
        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.agnostic_nms = agnostic_nms
        self.draw_box = draw_box
        self.augment = augment
        self.profile = profile
        self.half = device.type != 'cpu'  # half precision only supported on CUDA
        model: torch.nn.Module = attempt_load(model_path, #module_dir,
                                              map_location=device)  # load FP32 model  #         
        
        self.img_size = check_img_size(img_size, s=model.stride.max())  # check img_size 
        if self.half:  # 
            model.half()  # to FP16
        self.model = model
        # Get names and colors        
        self.names = ['insulatorburst','nest']
        print(self.names)
        self.colors = [[random.randint(50, 200) for _ in range(3)] for _ in range(len(self.names))]
        self.time = 0
        self.counter = 0


    def _detect(self, img0):
        self.model.eval()
        image = processImg(img0[:, :, :3], self.img_size)
        img = torch.from_numpy(image).to(self.device)  # 
        img = img.half() if self.half else img.float()  # 
        img /= 255.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)  # 
        # Inference
        det_time0 = time.time()
        with torch.no_grad():
            pred_res = self.model(img, augment=self.augment, visualize=False)[0]  # 
        det_time1 = time.time()
        det_time = det_time1 - det_time0
        det_time = int(det_time * 1000)
        self.time = self.time + det_time
        self.counter = self.counter + 1
        #print(self.time, self.counter)
        
        return img.shape, pred_res

    def detect(self, img0_file, use_cls):
        '''
        save_txt: 是否保存每一张图像识别结果图像及label
        save_yolo_flag： 保存yolo格式 or voc 格式
        '''
        #img0 = cv2.imread(img0_file)
        img0 = cv2.imdecode(np.fromfile(img0_file, dtype=np.uint8), -1)
        shape, pred_res = self._detect(img0)



        pred_res = non_max_suppression(pred_res, self.conf_thres, self.iou_thres, agnostic=self.agnostic_nms)
        det = pred_res[0]  # 
        all_result = []
        if det is None or len(det) == 0:
            return {}
        det[:, :4] = scale_coords(shape[2:], det[:, :4], img0.shape).round()
        if self.draw_box:
            for *xyxy, conf, cls in det:
                if int(cls) == use_cls:
                    label = self.names[int(cls)]

                    #print(self.names[int(cls)])
                    scores =  float('%.2f' % (conf))
                    all_result.append({'name': label,'score': scores,'xmin': int(xyxy[0]),'ymin': int(xyxy[1]),'xmax': int(xyxy[2]),'ymax': int(xyxy[3])})
                    #plot_one_box(xyxy, img0, label=label, color=self.colors[int(cls)], line_thickness=3)
            #cv2.imwrite('/data/zhangqinlu/workspace/nanwang_comptetion/my_scripts/yolov5_infer/data/1_r.jpg',img0)           	          	
        return all_result
        
        
    def speed_test(self):
        print(self.time)
        print(self.counter)
        speed_all = self.time / self.counter
        print(speed_all)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_path', type=str, help='model path', default='/data/zhangqinlu/workspace/nanwang_comptetion/my_scripts/yolov5_infer/weights/jyz_nc/jyz_nc.pt')
    parser.add_argument('--img_dir', type=str,  default='/data/zhangqinlu/workspace/nanwang_comptetion/my_scripts/yolov5_infer/data/1.jpg',help='image dir')
    parser.add_argument('--device', type=str, default='8', help='gpu id')
    parser.add_argument('--model_size', type=int, default=1280, help='模型输入大小')
    parser.add_argument('--conf_thres', type=float, default=0.5, help='conf')
    parser.add_argument('--iou_thres', type=float, default=0.2, help='iou')
    parser.add_argument('--use_cls', type=int, default=1, help='0 绝缘子自爆 1 鸟巢')
    args = parser.parse_args()
    #detector = Detector(args.model_path, args.model_size, device=args.device)


    detector = Detector(args.model_path, args.model_size, conf_thres=args.conf_thres, iou_thres=args.conf_thres, device=args.device)
    img_file = args.img_dir
    result = detector.detect(img_file,  args.use_cls)
    print(result)
                        