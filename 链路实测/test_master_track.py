"""_build_master_track 真实字节验证：用 IndexTTS 实测 wav（float32/22050）转 48k PCM16 后拼母轨。

断言：母轨总长 = sample_count；单元首样本在偏移处逐样本相等；间隙为静音；manifest 登记 mixed；
变异检查：把偏移挪动 480 样本后必须检测到不等。
"""
import sys, tempfile, wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "server"))
from duocast.adapters.comfyui_tts import _parse_wav, _resample_linear, _write_pcm16_wav
from duocast.domain.audio import AudioTimeline, SynthesisUnit
from duocast.services.voice_svc import _build_master_track
from duocast.storage.artifacts import ArtifactStore

SRC = Path(__file__).resolve().parent / "_tts" / "tts_00001.wav"
rate, ch, raw = _parse_wav(SRC.read_bytes())
mono = _resample_linear(raw, rate, 48000)
assert ch == 1 and len(mono) > 288000, (ch, len(mono))
tmp = Path(tempfile.mkdtemp(prefix="master_check_"))

def unit_wav(name: str, samples: list[float]) -> tuple[str, int]:
    p = tmp / name
    n = _write_pcm16_wav(p, samples, 48000)
    return str(p), n

# 单元1：前 3 秒；单元2：第 3~6 秒
s1, n1 = unit_wav("u1.wav", mono[:144000])
s2, n2 = unit_wav("u2.wav", mono[144000:288000])

gap = 320 * 48
lead = 500 * 48
tl = AudioTimeline(revision_id="REV-TEST", sample_rate=48000)
tl.units = [SynthesisUnit(id="U1", turn_id="T1"), SynthesisUnit(id="U2", turn_id="T2")]
tl.units[0].sample_count = n1
tl.units[1].sample_count = n2
tl.unit_offsets = {"U1": lead, "U2": lead + n1 + gap}
tl.transition_gap_ms = [320]
tl.lead_in_ms = 500
tl.tail_out_ms = 300
tl.sample_count = lead + n1 + gap + n2 + 300 * 48

store = ArtifactStore(tmp / "manifest_root")
asset_id = _build_master_track(tl, {"U1": s1, "U2": s2}, "REV-TEST", store, tmp / "artifacts")

with wave.open(str(next((tmp / "artifacts" / "audio").glob("master_*.wav"))), "rb") as wf:
    assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 48000)
    frames = wf.readframes(wf.getnframes())
master = array("h", frames)
assert len(master) == tl.sample_count, (len(master), tl.sample_count)

u1 = array("h", Path(s1).read_bytes()[44:])  # 跳 44 字节 WAV 头
u2 = array("h", Path(s2).read_bytes()[44:])
assert master[tl.unit_offsets["U1"]:tl.unit_offsets["U1"] + n1] == u1, "U1 段不等"
assert master[tl.unit_offsets["U2"]:tl.unit_offsets["U2"] + n2] == u2, "U2 段不等"
silence_start = tl.unit_offsets["U1"] + n1
assert all(v == 0 for v in master[silence_start:tl.unit_offsets["U2"]]), "间隙非静音"
assert all(v == 0 for v in master[:lead]), "lead-in 非静音"

entry = store.by_id(asset_id)
assert entry and entry["kind"] == "mixed" and Path(entry["path"]).is_file()
print("OK asset=", asset_id, "samples=", len(master), "=", round(len(master)/48000, 2), "s")

# 变异检查：偏移错位必须被断言抓住
bad = AudioTimeline(revision_id="REV-TEST", sample_rate=48000)
bad.units = list(tl.units); bad.sample_count = tl.sample_count
bad.unit_offsets = {"U1": lead + 480, "U2": tl.unit_offsets["U2"]}
bad.transition_gap_ms = [320]
try:
    _build_master_track(bad, {"U1": s1, "U2": s2}, "REV-MUT", store, tmp / "artifacts2")
    # 错位后的母轨：在原期望偏移 lead 处读取，内容应与正确拼接不一致（证明主检查能捕捉偏移错误）
    r = array("h", wave.open(str(next((tmp / "artifacts2" / "audio").glob("master_*.wav"))), "rb").readframes(-1))[lead: lead + n1]
    assert r != u1, "变异检查失败：错位 480 样本后原偏移处竟然仍相等"
    print("OK 变异检查：错位 480 样本后 U1 段内容与正确拼接不一致（断言可捕捉）")
except AssertionError as e:
    print("FAIL", e); sys.exit(1)
