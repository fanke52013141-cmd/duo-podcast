"""Geometry for experimental native-pixel face-region expert generation."""
import math


def joint_crop(segments, native):
    width, height = native
    expanded = []
    for segment in segments:
        if segment['geometry']['native'][:2] != [width, height]:
            raise ValueError('ROI experiment requires one shared native geometry')
        for role in segment['sourceByRole'].values():
            for frame in role:
                x1, y1, x2, y2 = frame['faceBox']
                if not 0 <= x1 < x2 <= width or not 0 <= y1 < y2 <= height:
                    raise ValueError('Confirmed face box outside source')
                dx, dy = (x2-x1)*0.4, (y2-y1)*0.4
                expanded.append((x1-dx, y1-dy, x2+dx, y2+dy))
    if not expanded:
        raise ValueError('Confirmed face trajectories required')
    left = max(0, math.floor(min(b[0] for b in expanded)/16)*16)
    top = max(0, math.floor(min(b[1] for b in expanded)/16)*16)
    right = min(width, math.ceil(max(b[2] for b in expanded)/16)*16)
    bottom = min(height, math.ceil(max(b[3] for b in expanded)/16)*16)
    if right-left < 256 or bottom-top < 256 or any(v % 16 for v in (left, top, right, bottom)):
        raise ValueError('Joint ROI must be at least 256 square and aligned to 16 pixels')
    return [left, top, right-left, bottom-top]


def composite_frame(source, patch, crop, face_boxes):
    import numpy as np
    x, y, width, height = crop
    if (source.dtype != np.uint8 or patch.dtype != np.uint8 or source.ndim != 3
            or source.shape[2] != 3 or patch.shape != (height, width, 3)
            or x < 0 or y < 0 or y+height > source.shape[0] or x+width > source.shape[1]):
        raise ValueError('Native ROI patch must fit source exactly without resizing')
    yy, xx = np.mgrid[y:y+height, x:x+width]
    alpha = np.zeros((height, width), dtype=np.float32)
    if not face_boxes:
        raise ValueError('Face trajectories required for composition')
    for x1, y1, x2, y2 in face_boxes:
        if not x <= x1 < x2 <= x+width or not y <= y1 < y2 <= y+height:
            raise ValueError('Face box outside fixed ROI')
        radius_x, radius_y = (x2-x1)*0.7, (y2-y1)*0.7
        distance = np.sqrt(((xx-(x1+x2)/2)/radius_x)**2+((yy-(y1+y2)/2)/radius_y)**2)
        weight = np.clip((1-distance)/0.18, 0, 1)
        weight = weight*weight*(3-2*weight)
        alpha = np.maximum(alpha, weight)
    result = source.copy()
    original = source[y:y+height, x:x+width]
    result[y:y+height, x:x+width] = np.rint(original*(1-alpha[..., None])+patch*alpha[..., None]).astype(np.uint8)
    return result
