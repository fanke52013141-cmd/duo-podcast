from fastapi import APIRouter, HTTPException, Request
import asyncio
from pydantic import BaseModel, ConfigDict, Field
from ..services.expert_assembly import assembly_plan
from ..services.expert_candidates import current_project, verified_candidate
from ..services.render_inputs import dependency_hash
from ..services.render_svc import apply_output_version
from ..storage.project_store import ProjectRevisionConflict

router=APIRouter(prefix='/api/projects/{project_id}/expert-video',tags=['expert-video'])


@router.get('/assembly-plan')
def plan(project_id: str,request: Request):
    return assembly_plan(request.app.state,project_id)


class AssembleRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    assembly_id: str=Field(alias='assemblyId',pattern=r'^EA-[a-f0-9]{24}$')
    client_token: str=Field(alias='clientToken',pattern=r'^[A-Za-z0-9_-]{8,128}$')


@router.post('/assemble')
async def assemble(project_id: str,payload: AssembleRequest,request: Request):
    state=request.app.state
    current_project(state,project_id)
    for job in state.job_manager.list(project_id):
        if job.kind=='expert.assemble' and job.client_token==payload.client_token:
            if job.input_snapshot['assemblyPlan']['assemblyId']!=payload.assembly_id:
                raise HTTPException(409,'请求标识已用于其他合成计划')
            return {'jobId':job.id}
    current=await asyncio.to_thread(assembly_plan,state,project_id)
    if not current['ready']:
        raise HTTPException(422,current['reason'])
    if current['assemblyId']!=payload.assembly_id:
        raise HTTPException(409,'采用版本已变化，请重新检查合成范围')
    project=current_project(state,project_id)
    job=state.job_manager.submit(kind='expert.assemble',project_id=project_id,queue_class='cpu',
        client_token=payload.client_token,input_snapshot={'assemblyPlan':current,'dependencyHash':current['dependencyHash'],
        'revisionId':project.current_draft_revision})
    return {'jobId':job.id}


@router.get('/assemblies')
def results(project_id: str,request: Request):
    state=request.app.state
    project=current_project(state,project_id)
    selected=state.expert_version_store.read(project_id)
    return [{'jobId':j.id,'status':j.status.value,'stage':j.stage,'error':j.error,'result':j.result,
        'stale':j.input_snapshot['dependencyHash']!=dependency_hash(project)
             or j.input_snapshot['assemblyPlan']['selectionRevision']!=selected['revision']}
        for j in state.job_manager.list(project_id) if j.kind=='expert.assemble']


class AdoptAssemblyRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_project_revision: int=Field(alias='expectedProjectRevision',ge=0)
    reviewed: bool


@router.post('/assemblies/{job_id}/adopt')
async def adopt(project_id: str,job_id: str,payload: AdoptAssemblyRequest,request: Request):
    state=request.app.state
    project=current_project(state,project_id)
    if not payload.reviewed:
        raise HTTPException(422,'请先完整观看并确认整期成片')
    job=await asyncio.to_thread(verified_candidate,state,project_id,job_id,project,kind='expert.assemble')
    if (await asyncio.to_thread(assembly_plan,state,project_id)).get('assemblyId')!=job.input_snapshot['assemblyPlan']['assemblyId']:
        raise HTTPException(409,'窗口选择已改变，不能采用旧合成结果')
    project=current_project(state,project_id)
    if dependency_hash(project)!=job.input_snapshot['dependencyHash']:
        raise HTTPException(409,'工程已改变，请重新合成')
    if (project.output_manifest or {}).get('meta',{}).get('expertAssemblyJobId')==job.id:
        return {'version':project.output_manifest['version']}
    if payload.expected_project_revision!=project.revision:
        raise HTTPException(409,'工程版本已改变，请刷新')
    try:
        result={**job.result,'meta':{**job.result['meta'],'candidate':False}}
        version=apply_output_version(state.project_store,project,job.input_snapshot['revisionId'],result)
    except ProjectRevisionConflict as error:
        raise HTTPException(409,'工程版本已改变，请刷新') from error
    return {'version':version}
