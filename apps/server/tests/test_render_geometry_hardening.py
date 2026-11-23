"""CPU-only regressions for standard-render geometry and subprocess cleanup."""
from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path

import pytest

import duocast.adapters.comfyui_video as video_module
from duocast.adapters.comfyui_video import ComfyUIVideoProvider


def _decode_grayscale_png(path: Path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, width, height, compressed = 8, None, None, bytearray()
    while position < len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
        elif kind == b"IDAT":
            compressed.extend(payload)
        position += length + 12
    raw = zlib.decompress(bytes(compressed))
    stride = width + 1
    assert len(raw) == stride * height
    assert all(raw[row * stride] == 0 for row in range(height))
    return width, height, [raw[row * stride + 1:(row + 1) * stride] for row in range(height)]


def test_image_scale_geometry_matches_16x9_letterbox():
    assert ComfyUIVideoProvider.image_scale_geometry(1920, 1080) == {
        "width": 864, "height": 480,
        "contentX": 5, "contentY": 0,
        "contentWidth": 853, "contentHeight": 480,
    }


@pytest.mark.parametrize("source,expected", [
    ((1600, 1200), {"width": 640, "height": 480, "contentX": 0, "contentY": 0,
                    "contentWidth": 640, "contentHeight": 480}),
    ((2000, 1000), {"width": 960, "height": 480, "contentX": 0, "contentY": 0,
                    "contentWidth": 960, "contentHeight": 480}),
    ((1080, 1920), {"width": 480, "height": 864, "contentX": 0, "contentY": 5,
                    "contentWidth": 480, "contentHeight": 853}),
])
def test_image_scale_geometry_other_aspect_ratios(source, expected):
    assert ComfyUIVideoProvider.image_scale_geometry(*source) == expected


def test_image_scale_geometry_rejects_oversized_canvas():
    with pytest.raises(ValueError, match="安全尺寸上限"):
        ComfyUIVideoProvider.image_scale_geometry(100000, 100)


def test_mask_uses_actual_canvas_content_and_padding(tmp_path):
    path = tmp_path / "mask_A.png"
    geometry = ComfyUIVideoProvider.image_scale_geometry(1920, 1080)
    ComfyUIVideoProvider._mask_png(
        path, {"x": 0.1, "y": 0.0, "width": 0.2, "height": 1.0}, geometry)
    width, height, rows = _decode_grayscale_png(path)
    assert (width, height) == (864, 480)
    # Source x=0.1..0.3 maps into the 853px content at x-offset 5.
    assert rows[0][:90] == bytes(90)
    assert rows[0][90:261] == b"\xff" * 171
    assert rows[0][261:] == bytes(width - 261)
    assert rows[-1] == rows[0]


def test_mask_can_infer_geometry_from_adjacent_over_shoulder_source(tmp_path):
    source = tmp_path / "camera.png"
    ComfyUIVideoProvider._mask_png(
        source, {"x": 0, "y": 0, "width": 1, "height": 1},
        ComfyUIVideoProvider.image_scale_geometry(1600, 1200))
    assert ComfyUIVideoProvider._read_image_size(source) == (640, 480)
    mask = tmp_path / "camera_A.png"
    ComfyUIVideoProvider._mask_png(mask, {"x": 0, "y": 0, "width": 0.5, "height": 1})
    width, height, rows = _decode_grayscale_png(mask)
    assert (width, height) == (640, 480)
    assert rows[0] == b"\xff" * 320 + bytes(320)


def test_image_size_reads_jpeg_sof_without_scanning_payload(tmp_path):
    path = tmp_path / "camera.jpg"
    path.write_bytes(
        b"\xff\xd8\xff\xe0\x00\x04\x00\x00"
        b"\xff\xc0\x00\x08\x08\x01\xe0\x02\x80\x03\x01\x11\x00\xff\xd9")
    assert ComfyUIVideoProvider._read_image_size(path) == (640, 480)


def test_image_size_rejects_non_png_jpeg(tmp_path):
    path = tmp_path / "not-an-image.png"
    path.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="PNG/JPEG"):
        ComfyUIVideoProvider._read_image_size(path)


class _FakeProcess:
    def __init__(self, *, returncode=0, stderr=b"", wait_forever=False):
        self.returncode = returncode
        self.stderr = stderr
        self.wait_forever = wait_forever
        self.started = asyncio.Event()
        self.stopped = False

    async def communicate(self):
        self.started.set()
        if self.wait_forever:
            await asyncio.Event().wait()
        return b"stdout", self.stderr

    async def stop(self):
        self.stopped = True


def test_command_uses_owned_process_and_stops_after_success(monkeypatch):
    process = _FakeProcess()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(video_module, "spawn_owned", spawn)
    result = asyncio.run(ComfyUIVideoProvider._command("ffmpeg", "-version"))
    assert result == b"stdout"
    assert calls[0][0] == ("ffmpeg", "-version")
    assert process.stopped


def test_command_stops_process_tree_on_cancellation(monkeypatch):
    process = _FakeProcess(wait_forever=True)
    monkeypatch.setattr(video_module, "spawn_owned", lambda *a, **k: _async_value(process))

    async def exercise():
        task = asyncio.create_task(ComfyUIVideoProvider._command("ffmpeg", "-i", "slow"))
        await process.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.stopped

    asyncio.run(exercise())


def test_command_stops_process_tree_on_timeout(monkeypatch):
    process = _FakeProcess(wait_forever=True)
    monkeypatch.setattr(video_module, "spawn_owned", lambda *a, **k: _async_value(process))
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(ComfyUIVideoProvider._command("ffmpeg", "-i", "slow", timeout=0.01))
    assert process.stopped


def test_command_reports_failure_after_cleanup(monkeypatch):
    process = _FakeProcess(returncode=1, stderr=b"bad input")
    monkeypatch.setattr(video_module, "spawn_owned", lambda *a, **k: _async_value(process))
    with pytest.raises(RuntimeError, match="bad input"):
        asyncio.run(ComfyUIVideoProvider._command("ffmpeg", "-i", "bad"))
    assert process.stopped


async def _async_value(value):
    return value
