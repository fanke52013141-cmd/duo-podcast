/* ⓘ 悬浮说明：大段解释性文案收进悬浮提示（45号 §九红线14：安全/审看规则进 ⓘ 不删除）。
   同时提供原生 title（键盘/读屏可达）与 CSS 气泡（鼠标悬浮即时可见）。 */
export function InfoTip({ text, label }: { text: string; label?: string }) {
  return (
    <span
      className="hf-infotip"
      tabIndex={0}
      role="note"
      aria-label={label ?? '说明'}
      title={text}
      data-tip={text}
    >ⓘ</span>
  );
}