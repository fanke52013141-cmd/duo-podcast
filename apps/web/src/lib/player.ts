import { useSyncExternalStore } from 'react';
let audio: HTMLAudioElement | null = null;
let state = { assetId: '', playing: false, current: 0, duration: 0, error: '' };
const listeners = new Set<() => void>();
let playbackRequest = 0;
let selectionEnd: number | null = null;
const update = (patch: Partial<typeof state>) => { state = { ...state, ...patch }; listeners.forEach(f => f()); };
function element() {
  if (!audio) {
    audio = new Audio();
    const sync = () => {
      if (selectionEnd !== null && audio!.currentTime >= selectionEnd) {
        const end = selectionEnd; selectionEnd = null; audio!.pause(); audio!.currentTime = end;
      }
      update({ playing: !audio!.paused, current: audio!.currentTime, duration: Number.isFinite(audio!.duration) ? audio!.duration : 0 });
    };
    for (const event of ['timeupdate', 'play', 'pause', 'ended', 'loadedmetadata']) audio.addEventListener(event, sync);
    audio.addEventListener('error', () => update({ error: '音频无法读取，请检查媒体文件或重新生成。', playing: false }));
  }
  return audio;
}
export async function playAudio(assetId: string, seconds = 0, end?: number) {
  if (!Number.isFinite(seconds) || seconds < 0 || (end !== undefined && (!Number.isFinite(end) || end <= seconds))) return;
  const request = ++playbackRequest;
  selectionEnd = null;
  const el = element(); el.pause();
  if (state.assetId !== assetId) { el.src = `/api/artifacts/${encodeURIComponent(assetId)}/file`; update({ assetId, duration: 0 }); }
  update({ error: '', current: seconds, playing: false });
  try {
    el.currentTime = seconds;
    selectionEnd = end ?? null;
    await el.play();
  } catch {
    if (request === playbackRequest) update({ error: '播放未能开始，请点击播放重试或检查音频文件。', playing: false });
  }
}
export function pauseAudio() { ++playbackRequest; element().pause(); }
export function seekAudio(seconds: number) { if (!Number.isFinite(seconds)) return; selectionEnd = null; element().currentTime = Math.max(0, seconds); }
export function stopAudio() { ++playbackRequest; selectionEnd = null; if (audio) { audio.pause(); audio.currentTime = 0; update({current:0,playing:false}); } }
export function usePlayer() { return useSyncExternalStore(f => { listeners.add(f); return () => { listeners.delete(f); }; }, () => state); }
