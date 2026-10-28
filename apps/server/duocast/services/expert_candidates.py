"""Candidate contracts, with server-owned jobs and registered media as evidence."""
from pathlib import Path
from fastapi import HTTPException
from .render_inputs import dependency_hash


def current_project(state, project_id):
    project = state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, '工程不存在')
    return project


def verified_candidate(state, project_id, job_id, project, kind='expert.generate'):
    job = state.job_manager.get(job_id)
    if not job or job.project_id != project_id or job.kind != kind:
        raise HTTPException(404, '候选不存在')
    if job.input_snapshot['dependencyHash'] != dependency_hash(project):
        raise HTTPException(409, '候选已过期，请按当前工程重新生成')
    if job.status.value != 'succeeded' or not job.result or job.result.get('simulated'):
        raise HTTPException(422, '候选尚未成功生成真实媒体')
    ids = job.result.get('artifactIds', [])
    if len(ids) != 1:
        raise HTTPException(422, '候选媒体不完整')
    artifact = state.artifacts.by_id(ids[0])
    if (not artifact or artifact['kind'] != 'video' or not Path(artifact['path']).is_file()
            or state.artifacts._hash_file(artifact['path']) != artifact['fileHash']
            or artifact['dependencyHash'] != job.input_snapshot['dependencyHash']):
        raise HTTPException(422, '候选文件缺失或已改变，不能采用')
    return job
