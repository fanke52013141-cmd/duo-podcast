import { pauseAudio, playAudio, seekAudio, usePlayer } from '../lib/player';
export function AudioPlayer({ assetId }: { assetId: string }) {
  const p = usePlayer(); const active = p.assetId === assetId;
  return <div className="wu-row"><button className="wu-btn" onClick={() => active && p.playing ? pauseAudio() : void playAudio(assetId, active ? p.current : 0)}>{active && p.playing ? '暂停' : '连续试听'}</button>
    <input aria-label="音频播放位置" type="range" min="0" max={active ? p.duration || 1 : 1} step="0.01" value={active ? p.current : 0} disabled={!active} onChange={e=>seekAudio(Number(e.target.value))}/>
    <span>{active ? `${p.current.toFixed(1)} / ${p.duration.toFixed(1)} 秒` : '待播放'}</span>{p.error && <span role="alert">{p.error}</span>}</div>;
}
