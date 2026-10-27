import { useEffect, useRef, useState } from 'react';
import { audioPeaks, boundedSelection, readBoundedAudio } from '../lib/waveform';
import { playAudio, seekAudio, usePlayer } from '../lib/player';
import { Button } from './wu';

export function AudioWaveform({assetId}: {assetId: string}) {
  const [wave, setWave] = useState<{peaks:number[];duration:number}|null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [start, setStart] = useState(0), [end, setEnd] = useState(0);
  const controller = useRef<AbortController|null>(null);
  const player = usePlayer(), active = player.assetId === assetId;
  useEffect(() => {
    controller.current?.abort(); setWave(null); setError(''); setBusy(false); setStart(0); setEnd(0);
    return () => controller.current?.abort();
  }, [assetId]);
  const load = async () => {
    if (busy) return;
    const request = new AbortController(); controller.current = request; setBusy(true); setError('');
    let context: AudioContext|null = null;
    try {
      const response = await fetch(`/api/artifacts/${encodeURIComponent(assetId)}/file`, {signal:request.signal});
      const data = await readBoundedAudio(response, request.signal);
      if (request.signal.aborted) return;
      context = new AudioContext();
      const decoded = await context.decodeAudioData(data);
      if (!Number.isFinite(decoded.duration) || decoded.duration > 301) throw new Error('波形仅支持最多五分钟音频');
      const peaks = audioPeaks(Array.from({length:decoded.numberOfChannels}, (_, i) => decoded.getChannelData(i)));
      if (!request.signal.aborted) {setWave({peaks,duration:decoded.duration}); setEnd(decoded.duration);}
    } catch (e) { if (!request.signal.aborted) setError((e as Error).message); }
    finally { if (context) await context.close().catch(()=>{}); if (!request.signal.aborted) setBusy(false); }
  };
  const selection = wave && boundedSelection(start, end, wave.duration);
  const locate = (seconds:number) => { if (active) seekAudio(seconds); else void playAudio(assetId,seconds); };
  return <section className="audio-waveform" aria-label="真实音频波形与选段">
    {!wave && <Button size="sm" busy={busy} onClick={()=>void load()}>加载真实音频波形</Button>}
    {error && <p role="alert">{error}</p>}
    {wave && <>
      <svg viewBox="0 0 960 80" role="img" aria-label="解码音频振幅波形" onClick={e=>{
        const bounds=e.currentTarget.getBoundingClientRect();
        if(bounds.width) locate(Math.min(wave.duration,Math.max(0,(e.clientX-bounds.left)/bounds.width*wave.duration)));
      }}>
        {selection && <rect x={selection[0]/wave.duration*960} width={(selection[1]-selection[0])/wave.duration*960} height="80" fill="currentColor" opacity=".12"/>}
        {wave.peaks.map((peak,i)=><line key={i} x1={(i+.5)*960/wave.peaks.length} x2={(i+.5)*960/wave.peaks.length} y1={40-peak*38} y2={40+peak*38} stroke="currentColor" strokeWidth="2"/>)}
        {active && <line x1={player.current/wave.duration*960} x2={player.current/wave.duration*960} y1="0" y2="80" stroke="#d18b26" strokeWidth="3"/>}
      </svg>
      <label>波形定位（秒）<input aria-label="波形定位秒" type="range" min="0" max={wave.duration} step="0.01" value={active?Math.min(player.current,wave.duration):0} onChange={e=>locate(Number(e.target.value))}/></label>
      <div className="wu-row">
        <label>选段起点（秒）<input aria-label="选段起点秒" type="number" min="0" max={wave.duration} step="0.01" value={Number.isFinite(start)?start:''} onChange={e=>setStart(e.target.valueAsNumber)}/></label>
        <label>选段终点（秒）<input aria-label="选段终点秒" type="number" min="0" max={wave.duration} step="0.01" value={Number.isFinite(end)?end:''} onChange={e=>setEnd(e.target.valueAsNumber)}/></label>
        <Button size="sm" disabled={!selection} onClick={()=>selection&&void playAudio(assetId,selection[0],selection[1])}>试听选段</Button>
      </div>
      {!selection && <p role="alert">请填写有效的选段范围，终点须晚于起点且不超过音频时长。</p>}
      <p className="wu-caption">波形来自文件实际解码，点击波形或使用定位滑杆跳转；选段到终点自动暂停。时长 {wave.duration.toFixed(2)} 秒。</p>
    </>}
  </section>;
}
