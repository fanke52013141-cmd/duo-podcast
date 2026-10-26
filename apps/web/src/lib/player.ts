import { useSyncExternalStore } from 'react';
let audio: HTMLAudioElement | null = null;
let state = { assetId: '', playing: false, current: 0, duration: 0, error: '' };
const listeners = new Set<() => void>();
const update = (patch: Partial<typeof state>) => { state = { ...state, ...patch }; listeners.forEach(f => f()); };
function element() {
  if (!audio) {
    audio = new Audio();
    const sync = () => update({ playing: !audio!.paused, current: audio!.currentTime, duration: Number.isFinite(audio!.duration) ? audio!.duration : 0 });
    for (const event of ['timeupdate', 'play', 'pause', 'ended', 'loadedmetadata']) audio.addEventListener(event, sync);
    audio.addEventListener('error', () => update({ error: '音频无法读取，请检查媒体文件或重新生成。', playing: false }));
  }
  return audio;
}
export async function playAudio(assetId: string, seconds = 0) {
  const el = element(); el.pause();
  if (state.assetId !== assetId) { el.src = `/api/artifacts/${encodeURIComponent(assetId)}/file`; update({ assetId, error: '', current: seconds }); }
  el.currentTime = seconds;
  try { await el.play(); } catch { update({ error: '播放未能开始，请点击播放重试或检查音频文件。' }); }
}
export function pauseAudio() { element().pause(); }
export function seekAudio(seconds: number) { element().currentTime = Math.max(0, seconds); }
export function stopAudio() { if (audio) { audio.pause(); audio.currentTime = 0; } }
export function usePlayer() { return useSyncExternalStore(f => { listeners.add(f); return () => { listeners.delete(f); }; }, () => state); }
