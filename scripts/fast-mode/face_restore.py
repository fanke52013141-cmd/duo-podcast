"""Offline CodeFormer face restoration for generated mouth crops.

Self-contained inference (no basicsr/facexlib/network dependencies). Architecture
vendored from sczhou/CodeFormer (S-Lab License 1.0); weights are installed at
models/codeformer/codeformer.pth and never downloaded at runtime.

Only the generated 256x256 face is restored, before it is blended back into the
frame. Fidelity w follows CodeFormer semantics: 0 = strongest restoration,
1 = closest to the input identity. Default 0.7 keeps identity while recovering
teeth/lip detail that the SD-VAE decode smears.
"""
import math
from pathlib import Path

import cv2
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F

DEFAULT_FIDELITY = 0.7


def restore_fidelity(request):
    """None = disabled; float in [0,1] = CodeFormer fidelity weight."""
    value = request.get('faceRestore', DEFAULT_FIDELITY)
    if value is None or value == 'off' or value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError('faceRestore 必须为 off 或 0–1 的保真度')
    return float(value)


# ---------------- vendored VQGAN backbone (inference subset) ----------------

def normalize(in_channels):
    return nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)


@torch.jit.script
def swish(x):
    return x * torch.sigmoid(x)


class VectorQuantizer(nn.Module):
    def __init__(self, codebook_size, emb_dim, beta):
        super().__init__()
        self.codebook_size = codebook_size
        self.emb_dim = emb_dim
        self.beta = beta
        self.embedding = nn.Embedding(self.codebook_size, self.emb_dim)

    def get_codebook_feat(self, indices, shape):
        indices = indices.view(-1, 1)
        min_encodings = torch.zeros(indices.shape[0], self.codebook_size).to(indices)
        min_encodings.scatter_(1, indices, 1)
        z_q = torch.matmul(min_encodings.float(), self.embedding.weight)
        if shape is not None:
            z_q = z_q.view(shape).permute(0, 3, 1, 2).contiguous()
        return z_q


class Downsample(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=2, padding=0)

    def forward(self, x):
        x = F.pad(x, (0, 1, 0, 1), mode='constant', value=0)
        return self.conv(x)


class Upsample(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        return self.conv(F.interpolate(x, scale_factor=2.0, mode='nearest'))


class ResBlock(nn.Module):
    def __init__(self, in_channels, out_channels=None):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = in_channels if out_channels is None else out_channels
        self.norm1 = normalize(in_channels)
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1)
        self.norm2 = normalize(out_channels)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1)
        if self.in_channels != self.out_channels:
            self.conv_out = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x_in):
        x = swish(self.norm1(x_in))
        x = self.conv1(x)
        x = swish(self.norm2(x))
        x = self.conv2(x)
        if self.in_channels != self.out_channels:
            x_in = self.conv_out(x_in)
        return x + x_in


class AttnBlock(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.norm = normalize(in_channels)
        self.q = nn.Conv2d(in_channels, in_channels, 1)
        self.k = nn.Conv2d(in_channels, in_channels, 1)
        self.v = nn.Conv2d(in_channels, in_channels, 1)
        self.proj_out = nn.Conv2d(in_channels, in_channels, 1)

    def forward(self, x):
        h_ = self.norm(x)
        q, k, v = self.q(h_), self.k(h_), self.v(h_)
        b, c, h, w = q.shape
        q = q.reshape(b, c, h * w).permute(0, 2, 1)
        k = k.reshape(b, c, h * w)
        w_ = torch.bmm(q, k) * (int(c) ** (-0.5))
        w_ = F.softmax(w_, dim=2)
        v = v.reshape(b, c, h * w)
        h_ = torch.bmm(v, w_.permute(0, 2, 1)).reshape(b, c, h, w)
        return x + self.proj_out(h_)


class Encoder(nn.Module):
    def __init__(self, in_channels, nf, emb_dim, ch_mult, num_res_blocks, resolution, attn_resolutions):
        super().__init__()
        curr_res = resolution
        in_ch_mult = (1,) + tuple(ch_mult)
        blocks = [nn.Conv2d(in_channels, nf, kernel_size=3, stride=1, padding=1)]
        for i in range(len(ch_mult)):
            block_in_ch = nf * in_ch_mult[i]
            block_out_ch = nf * ch_mult[i]
            for _ in range(num_res_blocks):
                blocks.append(ResBlock(block_in_ch, block_out_ch))
                block_in_ch = block_out_ch
                if curr_res in attn_resolutions:
                    blocks.append(AttnBlock(block_in_ch))
            if i != len(ch_mult) - 1:
                blocks.append(Downsample(block_in_ch))
                curr_res //= 2
        blocks += [ResBlock(block_in_ch, block_in_ch), AttnBlock(block_in_ch),
                   ResBlock(block_in_ch, block_in_ch), normalize(block_in_ch),
                   nn.Conv2d(block_in_ch, emb_dim, kernel_size=3, stride=1, padding=1)]
        self.blocks = nn.ModuleList(blocks)

    def forward(self, x):
        for block in self.blocks:
            x = block(x)
        return x


class Generator(nn.Module):
    def __init__(self, nf, emb_dim, ch_mult, res_blocks, img_size, attn_resolutions):
        super().__init__()
        block_in_ch = nf * ch_mult[-1]
        curr_res = img_size // 2 ** (len(ch_mult) - 1)
        blocks = [nn.Conv2d(emb_dim, block_in_ch, kernel_size=3, stride=1, padding=1),
                  ResBlock(block_in_ch, block_in_ch), AttnBlock(block_in_ch),
                  ResBlock(block_in_ch, block_in_ch)]
        for i in reversed(range(len(ch_mult))):
            block_out_ch = nf * ch_mult[i]
            for _ in range(res_blocks):
                blocks.append(ResBlock(block_in_ch, block_out_ch))
                block_in_ch = block_out_ch
                if curr_res in attn_resolutions:
                    blocks.append(AttnBlock(block_in_ch))
            if i != 0:
                blocks.append(Upsample(block_in_ch))
                curr_res *= 2
        blocks += [normalize(block_in_ch), nn.Conv2d(block_in_ch, 3, kernel_size=3, stride=1, padding=1)]
        self.blocks = nn.ModuleList(blocks)

    def forward(self, x):
        for block in self.blocks:
            x = block(x)
        return x


class VQAutoEncoder(nn.Module):
    def __init__(self, img_size, nf, ch_mult, res_blocks=2, attn_resolutions=(16,), codebook_size=1024, emb_dim=256):
        super().__init__()
        self.encoder = Encoder(3, nf, emb_dim, ch_mult, res_blocks, img_size, attn_resolutions)
        self.quantize = VectorQuantizer(codebook_size, emb_dim, 0.25)
        self.generator = Generator(nf, emb_dim, ch_mult, res_blocks, img_size, attn_resolutions)


# ---------------- vendored CodeFormer transformer ----------------

def calc_mean_std(feat, eps=1e-5):
    b, c = feat.size()[:2]
    feat_var = feat.view(b, c, -1).var(dim=2) + eps
    feat_std = feat_var.sqrt().view(b, c, 1, 1)
    feat_mean = feat.view(b, c, -1).mean(dim=2).view(b, c, 1, 1)
    return feat_mean, feat_std


def adaptive_instance_normalization(content_feat, style_feat):
    size = content_feat.size()
    style_mean, style_std = calc_mean_std(style_feat)
    content_mean, content_std = calc_mean_std(content_feat)
    normalized = (content_feat - content_mean.expand(size)) / content_std.expand(size)
    return normalized * style_std.expand(size) + style_mean.expand(size)


class TransformerSALayer(nn.Module):
    def __init__(self, embed_dim, nhead=8, dim_mlp=2048):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(embed_dim, nhead, dropout=0.0)
        self.linear1 = nn.Linear(embed_dim, dim_mlp)
        self.linear2 = nn.Linear(dim_mlp, embed_dim)
        self.norm1 = nn.LayerNorm(embed_dim)
        self.norm2 = nn.LayerNorm(embed_dim)

    def forward(self, tgt, query_pos=None):
        tgt2 = self.norm1(tgt)
        q = k = tgt2 if query_pos is None else tgt2 + query_pos
        tgt2 = self.self_attn(q, k, value=tgt2)[0]
        tgt = tgt + tgt2
        tgt2 = self.norm2(tgt)
        tgt2 = self.linear2(F.gelu(self.linear1(tgt2)))
        return tgt + tgt2


class Fuse_sft_block(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.encode_enc = ResBlock(2 * in_ch, out_ch)
        self.scale = nn.Sequential(nn.Conv2d(in_ch, out_ch, 3, padding=1), nn.LeakyReLU(0.2, True),
                                   nn.Conv2d(out_ch, out_ch, 3, padding=1))
        self.shift = nn.Sequential(nn.Conv2d(in_ch, out_ch, 3, padding=1), nn.LeakyReLU(0.2, True),
                                   nn.Conv2d(out_ch, out_ch, 3, padding=1))

    def forward(self, enc_feat, dec_feat, w=1):
        enc_feat = self.encode_enc(torch.cat([enc_feat, dec_feat], dim=1))
        return dec_feat + w * (dec_feat * self.scale(enc_feat) + self.shift(enc_feat))


class CodeFormer(VQAutoEncoder):
    def __init__(self, dim_embd=512, n_head=8, n_layers=9, codebook_size=1024, latent_size=256,
                 connect_list=('32', '64', '128', '256')):
        super().__init__(512, 64, [1, 2, 2, 4, 4, 8], 2, [16], codebook_size)
        self.connect_list = list(connect_list)
        self.position_emb = nn.Parameter(torch.zeros(latent_size, dim_embd))
        self.feat_emb = nn.Linear(256, dim_embd)
        self.ft_layers = nn.Sequential(*[TransformerSALayer(dim_embd, n_head, dim_embd * 2) for _ in range(n_layers)])
        self.idx_pred_layer = nn.Sequential(nn.LayerNorm(dim_embd), nn.Linear(dim_embd, codebook_size, bias=False))
        self.channels = {'16': 512, '32': 256, '64': 256, '128': 128, '256': 128, '512': 64}
        self.fuse_encoder_block = {'512': 2, '256': 5, '128': 8, '64': 11, '32': 14, '16': 18}
        self.fuse_generator_block = {'16': 6, '32': 9, '64': 12, '128': 15, '256': 18, '512': 21}
        self.fuse_convs_dict = nn.ModuleDict()
        for f_size in self.connect_list:
            self.fuse_convs_dict[f_size] = Fuse_sft_block(self.channels[f_size], self.channels[f_size])

    def forward(self, x, w=0, adain=True):
        enc_feat_dict = {}
        out_list = [self.fuse_encoder_block[f] for f in self.connect_list]
        for i, block in enumerate(self.encoder.blocks):
            x = block(x)
            if i in out_list:
                enc_feat_dict[str(x.shape[-1])] = x.clone()
        lq_feat = x
        pos_emb = self.position_emb.unsqueeze(1).repeat(1, x.shape[0], 1)
        query_emb = self.feat_emb(lq_feat.flatten(2).permute(2, 0, 1))
        for layer in self.ft_layers:
            query_emb = layer(query_emb, query_pos=pos_emb)
        logits = self.idx_pred_layer(query_emb).permute(1, 0, 2)
        soft_one_hot = F.softmax(logits, dim=2)
        _, top_idx = torch.topk(soft_one_hot, 1, dim=2)
        quant_feat = self.quantize.get_codebook_feat(top_idx, shape=[x.shape[0], 16, 16, 256])
        if adain:
            quant_feat = adaptive_instance_normalization(quant_feat, lq_feat)
        x = quant_feat
        fuse_list = [self.fuse_generator_block[f] for f in self.connect_list]
        for i, block in enumerate(self.generator.blocks):
            x = block(x)
            if i in fuse_list:
                f_size = str(x.shape[-1])
                if w > 0:
                    x = self.fuse_convs_dict[f_size](enc_feat_dict[f_size].detach(), x, w)
        return x


class FaceRestorer:
    """Restore generated 256x256 BGR mouth faces to detail-rich 512x512 BGR.

    fp32 inference: CodeFormer's transformer is sensitive to fp16 rounding and
    the per-batch cost is small next to the MuseTalk UNet pass.
    """

    def __init__(self, weights, device='cuda'):
        weights = Path(weights)
        if not weights.is_file():
            raise ValueError(f'人脸修复模型缺失: {weights}')
        checkpoint = torch.load(weights, map_location='cpu', weights_only=True)
        state = checkpoint.get('params_ema') or checkpoint.get('params')
        if state is None:
            raise ValueError('人脸修复模型权重格式无效')
        self.net = CodeFormer()
        self.net.load_state_dict(state, strict=True)
        self.net.eval().to(device)
        for param in self.net.parameters():
            param.requires_grad = False
        self.device = device
        # Small GPU batches keep the restoration footprint ~1.6GB so it can share
        # the card with MuseTalk; CPU has no such pressure and batches wider.
        self.chunk = 2 if str(device).startswith('cuda') else 8

    @torch.no_grad()
    def restore_batch(self, faces, fidelity):
        """faces: iterable of 256x256 uint8 BGR. Returns list of 512x512 uint8 BGR."""
        faces = list(faces)
        if not faces:
            return []
        outputs = []
        for first in range(0, len(faces), self.chunk):
            chunk = faces[first:first + self.chunk]
            batch = np.stack([cv2.resize(f, (512, 512), interpolation=cv2.INTER_LANCZOS4) for f in chunk])
            x = torch.from_numpy(batch[..., ::-1].copy()).permute(0, 3, 1, 2).float().to(self.device)
            x = x / 255.0 * 2.0 - 1.0
            out = self.net(x, w=fidelity, adain=True)
            out = (out.clamp(-1, 1) + 1) / 2 * 255
            out = out.round().to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()[..., ::-1]
            outputs.extend(out)
        return outputs