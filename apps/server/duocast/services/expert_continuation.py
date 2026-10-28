"""Freeze adopted native cores for continuation; sampling is only for missing windows."""
from copy import deepcopy
from fastapi import HTTPException
from .expert_candidates import verified_candidate
from .expert_assembly import checked_artifact
from .expert_preview import digest


def freeze_reuse(state,project,plan):
    selection=state.expert_version_store.read(project.id)
    plan=deepcopy(plan)
    reuse={}
    if selection['selected'] and selection['snapshotId']!=plan['snapshotId']:
        raise HTTPException(409,'已采用视频与当前运行配置不同，请恢复匹配配置或重新生成预览')
    jobs={}
    for window in plan['windows']:
        job_id=selection['selected'].get(window['windowId'])
        if not job_id:continue
        if job_id not in jobs:jobs[job_id]=verified_candidate(state,project.id,job_id,project)
        job=jobs[job_id]
        source_plan=job.input_snapshot['expertSelection']
        if source_plan['snapshotId']!=plan['snapshotId']:
            raise HTTPException(409,'已采用窗口快照不一致')
        spec=next((w['segment'] for w in source_plan['windows'] if w['windowId']==window['windowId']),None)
        record=next((w for w in job.result.get('meta',{}).get('windowArtifacts',[]) if w['windowId']==window['windowId']),None)
        if not record or not spec:
            raise HTTPException(422,'旧候选缺少原生片段，不能用于续算')
        baseline=deepcopy(window['segment'])
        baseline.update(seed=spec['seed'],dependencyHash=spec['dependencyHash'])
        if baseline!=spec or any(record.get(k)!=spec[k] for k in ('dependencyHash','outputStartFrame','outputEndFrame')):
            raise HTTPException(409,'已采用片段与冻结整期计划不兼容')
        try:entry=checked_artifact(state.artifacts,record['artifactId'],record['fileHash'])
        except ValueError as error:raise HTTPException(422,str(error)) from error
        if entry['kind']!='video' or entry['dependencyHash']!=spec['dependencyHash']:
            raise HTTPException(422,'原生片段登记身份不一致')
        window['segment']=deepcopy(spec)
        reuse[window['windowId']]={**record,'sourceJobId':job_id}
    plan.update(reuseSelectionRevision=selection['revision'],reuseWindows=reuse,
        reusedWindowCount=len(reuse),generateWindowCount=len(plan['windows'])-len(reuse))
    plan['planId']='EP-'+digest({'selection':plan['planId'],'reuse':reuse,'revision':selection['revision']})[:24]
    return plan


def validate_continuation(selection,baseline_windows):
    """Worker boundary: a reused slot may only change seed and sampling dependency."""
    reuse=selection.get('reuseWindows',{})
    if set(reuse)-{w['windowId'] for w in baseline_windows}:raise ValueError('Unexpected reused window')
    expected=deepcopy(baseline_windows)
    actual={w['windowId']:w for w in selection['windows']}
    for window in expected:
        id=window['windowId']
        if id in reuse:
            adopted=actual[id]['segment']
            window['segment'].update(seed=adopted['seed'],dependencyHash=adopted['dependencyHash'])
            if any(reuse[id].get(k)!=adopted[k] for k in ('dependencyHash','outputStartFrame','outputEndFrame')):
                raise ValueError('Reused window identity changed')
    if expected!=selection['windows']:raise ValueError('Continuation modified frozen content or window clock')
