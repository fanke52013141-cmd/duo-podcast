"""Compose a sampled ROI core back into its unchanged full native reference."""
import json
from ..domain.motion_pack import content_hash


async def composite_core(provider,job,segment,prepared,core,record,folder):
    key=content_hash({'segment':segment,'coreHash':record['coreHash'],'reference':prepared['fullReference'],
        'crop':prepared['roiCrop'],'recipe':'confirmed-face-ellipses-1.4x-smooth-edge-v1'})
    work=folder/'composited'/key;work.mkdir(parents=True,exist_ok=True)
    final=work/'core.mp4';artifact_id='VID-EXPERT-ROI-CORE-'+key[:32]
    existing=provider.artifacts.by_id(artifact_id)
    if existing:
        provider._artifact(artifact_id,{'video'})
    else:
        partial=work/'core.partial.mp4'
        payload={'segment':segment,'prepared':prepared,'core':{'path':str(core),'fileHash':record['coreHash']},'output':str(partial)}
        request=work/'request.json';request.write_text(json.dumps(payload),encoding='utf-8')
        report=json.loads(await provider._command(provider.runtime/'venv/Scripts/python.exe','-B',provider.runtime/'expert_roi_media.py',request,timeout=300))
        if not report.get('outsideCropExactBeforeEncoding') or report['fileHash']!=provider.artifacts._hash_file(str(partial)):
            raise ValueError('ROI native composition evidence missing')
        await provider._video(partial,segment['outputEndFrame']-segment['outputStartFrame'])
        provider.manager.checkpoint(job);partial.replace(final)
        existing=provider.artifacts.register(artifact_id,'video',str(final),segment['dependencyHash'],
            {'source':'sageRoiCompositedCore','recipe':key,'spatialResize':False},'expert-sage-roi-v1')
    return final,{**record,'roiCoreArtifactId':record['coreArtifactId'],'coreArtifactId':artifact_id,
        'coreHash':existing['fileHash'],'roiComposite':True}
