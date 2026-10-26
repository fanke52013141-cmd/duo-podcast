import { usePatchProject } from '../lib/api';
import type { Project } from '../lib/types';
import { Alert } from './wu';
export function RenderSettings({ project }: { project: Project }) {
  const save = usePatchProject();
  const spec = project.renderSpec ?? {};
  const update = (patch: NonNullable<Project['renderSpec']>) => save.mutate({projectId:project.id,expectedRevision:project.revision,patch:{renderSpec:{...spec,...patch}}});
  return <section className="hf-block"><h4>样片与成片共用规格</h4><div className="hf-fld"><label>分辨率</label><select className="wu-input" aria-label="共用分辨率" value={spec.resolution??'854x480'} onChange={e=>update({resolution:e.target.value})}><option value="854x480">480p</option><option value="1280x720">720p</option><option value="1920x1080">1080p</option></select></div>
    <div className="hf-fld"><label>帧率</label><select className="wu-input" aria-label="共用帧率" value={spec.fps??25} onChange={e=>update({fps:Number(e.target.value)})}><option value="25">25 fps</option><option value="30">30 fps</option></select></div>
    {project.generationMode==='fast'&&<div className="hf-fld"><label>增强</label><select className="wu-input" aria-label="共用增强档位" value={spec.fastQuality??'original'} onChange={e=>update({fastQuality:e.target.value as 'original'|'clear'|'crisp'})}><option value="original">原始</option><option value="clear">清晰</option><option value="crisp">更清晰</option></select></div>}
    <p className="wu-caption">输出尺寸不代表素材原生清晰度。编码档位变化保留内容确认；角色、动作和构图改变后需重新检查。</p>
    {save.error&&<Alert tone="danger">{save.error.message}</Alert>}
  </section>;
}
