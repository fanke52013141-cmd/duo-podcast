"""Pure geometry for compositing at export detail before final camera crops."""
import hashlib
import json
import math

def composite_size(source_width, source_height, export_width, export_height):
    if min(source_width,source_height,export_width,export_height)<=0:
        raise ValueError('Frame dimensions must be positive')
    scale=max(1.0,export_width/(2*source_width),export_height/source_height)
    return (2*math.ceil(source_width*scale/2),2*math.ceil(source_height*scale/2))

def project_box(box, source_size, target_size):
    sw,sh=source_size;tw,th=target_size
    x,y,x2,y2=box
    result=(max(0,round(x*tw/sw)),max(0,round(y*th/sh)),min(tw,round(x2*tw/sw)),min(th,round(y2*th/sh)))
    if result[2]<=result[0] or result[3]<=result[1]:raise ValueError('Invalid face region')
    return result

def preparation_key(video, coordinates, vae_config, vae_weights):
    """Cache changes when source motion, boxes or VAE installation changes."""
    digest=hashlib.sha256()
    with video.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
    digest.update(json.dumps(coordinates,separators=(',',':')).encode())
    digest.update(vae_config.read_bytes())
    stat=vae_weights.stat()
    digest.update(f'vae-preparation-v2:{stat.st_size}:{stat.st_mtime_ns}'.encode())
    return digest.hexdigest()[:24]
