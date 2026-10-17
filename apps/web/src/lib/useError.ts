/* ============================================================================
   页面级错误状态（第五轮实测 A9）：
   - 错误横幅 12s 后自动清除，不再永久驻留遮挡后续成功反馈；
   - dismiss() 供 Alert 关闭按钮立即清除。
   ========================================================================== */
import { useCallback, useRef, useState } from 'react';

const AUTO_CLEAR_MS = 12000;

export function useError(): {
  error: string | null;
  setError: (msg: string | null) => void;
  dismiss: () => void;
} {
  const [error, setErrorState] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const setError = useCallback((msg: string | null) => {
    if (timer.current) window.clearTimeout(timer.current);
    setErrorState(msg);
    if (msg) timer.current = window.setTimeout(() => setErrorState(null), AUTO_CLEAR_MS);
  }, []);

  const dismiss = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current);
    setErrorState(null);
  }, []);

  return { error, setError, dismiss };
}
