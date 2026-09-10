/* ============================================================================
   DuoCast 前端 · wu-* 组件封装（workspace-ui 基线类名的 React 落位）
   所有组件输出类名即基线 CSS，不引入第二套样式；图标为基线 24 viewBox / 1.75 stroke。
   ========================================================================== */
import React, { useId } from 'react';

/* ---------------- Icon（基线图标集子集，按需扩充） ---------------- */
export type IconName =
  | 'plus' | 'folder' | 'more' | 'close' | 'check' | 'play' | 'stop' | 'arrowRight'
  | 'arrowLeft' | 'download' | 'trash' | 'edit' | 'search' | 'gear' | 'wand' | 'sparkle'
  | 'alert' | 'info' | 'clock' | 'save' | 'grip' | 'task' | 'image' | 'mic' | 'film'
  | 'link' | 'refresh' | 'chevronUp' | 'chevronDown' | 'pause';

const ICON_PATHS: Record<IconName, React.ReactNode> = {
  plus: <path d="M12 5v14M5 12h14" />,
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" />,
  more: <path d="M12 6.5h.01M12 12h.01M12 17.5h.01" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  check: <path d="M5 12.5l4.5 4.5L19 7" />,
  play: <path d="M7 5.5v13l11-6.5-11-6.5Z" />,
  stop: <path d="M7 7h10v10H7z" />,
  arrowRight: <path d="M5 12h14m-6-6 6 6-6 6" />,
  arrowLeft: <path d="M19 12H5m6-6-6 6 6 6" />,
  download: <path d="M12 4v11m-4.5-4.5L12 15l4.5-4.5M5 19h14" />,
  trash: <path d="M5 7h14M9 7V5h6v2m-8 0 1 12h8l1-12M10 11v5m4-5v5" />,
  edit: <path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3ZM13.5 6.5l3 3" />,
  search: <path d="M10.5 4a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13Zm6 12L20 20" />,
  gear: <path d="M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm7.4 3a7.4 7.4 0 0 0-.1-1.2l2-1.5-2-3.5-2.4 1a7.3 7.3 0 0 0-2-1.2L14.6 3h-4L10 4.6a7.3 7.3 0 0 0-2 1.2l-2.4-1-2 3.5 2 1.5a7.4 7.4 0 0 0 0 2.4l-2 1.5 2 3.5 2.4-1a7.3 7.3 0 0 0 2 1.2l.6 1.6h4l.6-1.6a7.3 7.3 0 0 0 2-1.2l2.4 1 2-3.5-2-1.5c.1-.4.1-.8.1-1.2Z" />,
  wand: <path d="M5 19 15.5 8.5M13 5l1.2 2.8L17 9l-2.8 1.2L13 13l-1.2-2.8L9 9l2.8-1.2L13 5ZM19 16l.7 1.6L21.3 18l-1.6.7L19 20.3l-.7-1.6L16.7 18l1.6-.7L19 16Z" />,
  sparkle: <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3ZM19 15l.9 2.4L22.3 18.3l-2.4.9L19 21.5l-.9-2.3-2.4-.9 2.4-.9L19 15Z" />,
  alert: <path d="M12 4 2.5 20h19L12 4Zm0 6v4m0 3h.01" />,
  info: <path d="M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 7.5V16m0-7h.01" />,
  clock: <path d="M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 4v4.5l3 2" />,
  save: <path d="M5 4h11l3 3v13H5V4Zm0 4h11v12H5V8Zm4 0h5v4H9V8Z" />,
  grip: <path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01" />,
  task: <path d="M8 5h9a2 2 0 0 1 2 2v12H6V7a2 2 0 0 1 2-2Zm0 0V3h5v2M9 10h6m-6 4h6m-6 4h3" />,
  image: <path d="M4 5h16v14H4V5Zm0 10 4-4 3 3 5-5 4 4" />,
  mic: <path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-6 9a6 6 0 0 0 12 0m-6 5v4m-3 0h6" />,
  film: <path d="M4 5h16v14H4V5Zm0 4h4m12 0h-4M4 13h4m12 0h-4m-8 4h8" />,
  link: <path d="M10 14a4 4 0 0 0 5.6.4l3-3a4 4 0 0 0-5.6-5.6l-1.5 1.5M14 10a4 4 0 0 0-5.6-.4l-3 3a4 4 0 0 0 5.6 5.6l1.5-1.5" />,
  refresh: <path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5" />,
  chevronUp: <path d="M6 14.5 12 8.5l6 6" />,
  chevronDown: <path d="M6 9.5l6 6 6-6" />,
  pause: <path d="M8 5v14M16 5v14" />,
};

export function Icon({ name, size = 20, className = '' }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg className={`wu-icon ${className}`} width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      {ICON_PATHS[name]}
    </svg>
  );
}

/* ---------------- Button ---------------- */
export type BtnVariant = 'primary' | 'brand' | 'secondary' | 'ghost' | 'danger';
export type BtnSize = 'sm' | 'md' | 'lg';

export function Button({
  variant = 'primary', size = 'md', shape, busy, disabled, icon, children, className = '', ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: BtnVariant; size?: BtnSize; shape?: 'pill'; busy?: boolean; icon?: IconName;
}) {
  const label = busy ? '处理中' : children;
  return (
    <button
      className={`wu-btn ${className}`}
      data-variant={variant}
      data-size={size}
      data-shape={shape}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      {...rest}
    >
      {icon && !busy && <Icon name={icon} size={16} />}
      <span className="wu-btn-label">{label}</span>
    </button>
  );
}

/* ---------------- Badge ---------------- */
export type Tone = 'muted' | 'success' | 'warning' | 'danger' | 'info' | 'brand' | 'speaker-b';

export function Badge({ tone = 'muted', children, icon }: { tone?: Tone; children: React.ReactNode; icon?: IconName }) {
  return (
    <span className="wu-badge" data-tone={tone}>
      {icon && <Icon name={icon} size={12} />}
      {children}
    </span>
  );
}

/* ---------------- Card / Alert / Empty ---------------- */
export function Card({
  children, interactive, onClick, className = '', style,
}: { children: React.ReactNode; interactive?: boolean; onClick?: () => void; className?: string; style?: React.CSSProperties }) {
  return (
    <div
      className={`wu-card ${className}`}
      data-variant={interactive ? 'interactive' : undefined}
      style={style}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } } : undefined}
    >
      {children}
    </div>
  );
}

export function Alert({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return (
    <div className="wu-alert" data-tone={tone}>
      {children}
    </div>
  );
}

export function Empty({ title, desc, action }: { title: string; desc?: string; action?: React.ReactNode }) {
  return (
    <div className="wu-empty">
      <div style={{ fontWeight: 700 }}>{title}</div>
      {desc && <p className="wu-caption" style={{ margin: '6px auto 14px', maxWidth: 420 }}>{desc}</p>}
      {action}
    </div>
  );
}

/* ---------------- Field / Input / Textarea ---------------- */
export function Field({ label, error, children, labelFor }: { label?: string; error?: string; children: React.ReactNode; labelFor?: string }) {
  return (
    <div className="wu-field">
      {label && <label className="wu-label" htmlFor={labelFor}>{label}</label>}
      {children}
      {error && <span className="wu-error">{error}</span>}
    </div>
  );
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  const { invalid, ...rest } = props as React.InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean };
  return <input className="wu-input" aria-invalid={invalid || undefined} {...rest} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className="wu-input" {...props} />;
}

/* ---------------- Choice（radio / checkbox） ---------------- */
export function Choice({
  type = 'radio', checked, onChange, label, name, value, disabled,
}: {
  type?: 'radio' | 'checkbox'; checked: boolean; onChange: (v: boolean) => void;
  label: React.ReactNode; name?: string; value?: string; disabled?: boolean;
}) {
  return (
    <label className="wu-choice">
      <input
        type={type}
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <label className="wu-choice">
      <input type="checkbox" className="wu-switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label && <span>{label}</span>}
    </label>
  );
}

/* ---------------- Tabs ---------------- */
export interface TabItem { id: string; label: React.ReactNode; disabled?: boolean; }

export function Tabs({ items, active, onSelect, idPrefix = 'tab' }: { items: TabItem[]; active: string; onSelect: (id: string) => void; idPrefix?: string }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '');
  return (
    <div>
      <div className="wu-tabs-list" role="tablist" data-wu-tabs>
        {items.map((it) => {
          const panelId = `${idPrefix}-${uid}-panel-${it.id}`;
          return (
            <button
              key={it.id}
              id={`${idPrefix}-${uid}-tab-${it.id}`}
              className="wu-tab"
              role="tab"
              aria-selected={active === it.id}
              aria-controls={panelId}
              tabIndex={active === it.id ? 0 : -1}
              disabled={it.disabled}
              onClick={() => onSelect(it.id)}
            >
              {it.label}
            </button>
          );
        })}
      </div>
      <div id={`${idPrefix}-${uid}-panel-${active}`} className="wu-tabpanel" role="tabpanel" tabIndex={0}>
        {items.find((i) => i.id === active)?.label != null ? null : null}
      </div>
    </div>
  );
}

/* ---------------- Modal（原生 dialog 落位） ---------------- */
export function Modal({
  open, onClose, title, children, footer, width,
}: { open: boolean; onClose: () => void; title: string; children: React.ReactNode; footer?: React.ReactNode; width?: number }) {
  const ref = React.useRef<HTMLDialogElement>(null);
  React.useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className="wu-dialog" style={width ? { width } : undefined} onClose={onClose} aria-label={title}>
      <div className="wu-dialog-body">
        <div className="wu-row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
          <h3 className="wu-heading">{title}</h3>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="关闭"><Icon name="close" size={16} /></Button>
        </div>
        {children}
      </div>
      {footer && <div className="wu-dialog-footer">{footer}</div>}
    </dialog>
  );
}

/* ---------------- 小工具 ---------------- */
export function Progress({ value, max = 100, tone = 'info' }: { value: number; max?: number; tone?: Tone }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="hf-prog" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${pct}%`, background: `var(--wu-semantic-${tone === 'info' ? 'info' : tone === 'success' ? 'success' : tone === 'warning' ? 'warning' : tone === 'danger' ? 'danger' : 'brand'})` }} />
    </div>
  );
}

export function SegControl({ options, value, onChange }: { options: { value: string; label: string }[]; value: string; onChange: (v: string) => void }) {
  return (
    <div className="hf-seg" role="group">
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={value === o.value} onClick={() => onChange(o.value)} title={o.label}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
