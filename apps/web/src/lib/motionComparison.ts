/** Synchronize a comparison using the processed clip as the only clock. */
export type ComparisonMedia = {
  currentTime: number;
  duration: number;
  playbackRate: number;
  paused: boolean;
  play(): Promise<void>;
  pause(): void;
};

export function sourceReference(value: unknown): {artifactId:string;fileHash:string}|null {
  if (!value || typeof value !== 'object') return null;
  const ref = value as Record<string,unknown>;
  if (typeof ref.artifactId !== 'string' || !ref.artifactId.trim() || ref.artifactId.length > 255 ||
      typeof ref.fileHash !== 'string' || !/^[a-f0-9]{64}$/.test(ref.fileHash)) return null;
  return {artifactId:ref.artifactId,fileHash:ref.fileHash};
}

/** The offset is source seconds minus processed seconds, not a speed mapping. */
export function createMotionComparison(leader:ComparisonMedia, follower:ComparisonMedia,
                                       notify:(message:string)=>void) {
  let offset = 0, generation = 0, disposed = false;
  const fail = (message:string) => {
    generation++;
    leader.pause();
    follower.pause();
    notify(message);
  };
  const align = (force=false):boolean => {
    if (disposed) return false;
    const target = leader.currentTime + offset;
    if (!Number.isFinite(target) || !Number.isFinite(follower.duration) || follower.duration <= 0) {
      fail('等待原始视频元数据后再同步。');
      return false;
    }
    // No clamping: a held endpoint could be mistaken for aligned motion.
    if (target < 0 || target >= follower.duration) {
      fail('当前位置超出原始视频范围，请调整偏移或跳到可对齐的位置。');
      return false;
    }
    try {
      if (force || Math.abs(follower.currentTime-target) > .08) follower.currentTime=target;
      if (follower.playbackRate !== leader.playbackRate) follower.playbackRate=leader.playbackRate;
    } catch {
      fail('原始视频无法定位，请确认素材可正常播放。');
      return false;
    }
    return true;
  };
  return {
    setOffset(seconds:number) {
      if (!Number.isFinite(seconds)) { fail('请输入有限的偏移秒数。'); return; }
      offset=seconds;
      align(true);
    },
    sync(force=false) { return align(force); },
    async play() {
      const current=++generation;
      if (!align(true)) return;
      try {
        await follower.play();
        if (disposed || current!==generation || leader.paused) follower.pause();
        else notify('');
      } catch {
        if (!disposed && current===generation) fail('原始视频无法播放，两个视频已暂停。');
      }
    },
    pause() { generation++; follower.pause(); },
    unavailable() { fail('原始视频不存在、未登记或无法解码。'); },
    ended() { fail('原始视频已播放到末尾，请调整偏移或跳到可对齐的位置。'); },
    dispose() { disposed=true; generation++; follower.pause(); },
  };
}
