"""Server-owned expert planning, queued execution and explicit candidate selection."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..services.expert_preview import digest, select_windows, timeline_turns
from ..services.expert_motion import build_expert_motion_plan
from ..services.motion_binding import validate_project_motion_binding
from ..services.motion_render_inputs import build_motion_worker_inputs, camera_edit_plan
from ..services.render_inputs import dependency_hash
from ..services.expert_candidates import current_project, verified_candidate

router = APIRouter(prefix='/api/projects/{project_id}/expert-video', tags=['expert-video'])


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    turn_ids: list[str] | None = Field(default=None, alias='turnIds', min_length=1, max_length=100)
    candidate_token: str | None = Field(default=None, alias='candidateToken', min_length=8, max_length=128)
    expected_dependency_hash: str | None = Field(default=None, alias='expectedDependencyHash', pattern=r'^[a-f0-9]{64}$')


def build_plan(project_id, payload, request, operation):
    state = request.app.state
    project = state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, '工程不存在')
    identity = dependency_hash(project)
    if payload.expected_dependency_hash is not None and payload.expected_dependency_hash != identity:
        raise HTTPException(409, '工程已改变，请重新获取预览计划')
    if project.motion_pack is None or project.audio_timeline is None:
        raise HTTPException(422, '请先采用动作包并生成配音')
    try:
        validate_project_motion_binding(project, state.motion_pack_store)
        data = project.model_dump(mode='json', by_alias=True)
        turns = timeline_turns(data)
        timeline = project.audio_timeline
        assets = build_motion_worker_inputs(state.motion_pack_store, state.artifacts,
            project.motion_pack.id, project.motion_pack.version, project.shot_style, turns,
            sample_rate=timeline.sample_rate, total_samples=timeline.sample_count, seed='1')
        edits = camera_edit_plan(assets, turns, timeline.sample_rate)
        full = build_expert_motion_plan(assets, edits, sample_rate=timeline.sample_rate,
                                       total_samples=timeline.sample_count, seed=1)
        result = select_windows(full, turns, turn_ids=payload.turn_ids, operation=operation,
                                candidate_token=payload.candidate_token)
    except (ValueError, KeyError, OSError, StopIteration) as error:
        raise HTTPException(422, str(error)) from error
    # Freeze the entire project, not just the selected range. Never trust client runtime/media paths.
    executor = getattr(state, 'expert_executor', None)
    engine_identity = executor.engine_fingerprint if executor else None
    profile=getattr(executor,'profile','expert-sage-roi-v1')
    sage_hash=getattr(executor,'sage_tree_hash',None)
    result.update(projectId=project_id, dependencyHash=identity, engineFingerprint=engine_identity, sageTreeHash=sage_hash,
                  snapshotId='ES-'+digest({'project': identity, 'fullPlan': result['sourcePlanHash'],
                                           'engine': engine_identity, 'profile':profile,'sageTreeHash':sage_hash})[:24],
                  executionAvailable=getattr(state, 'expert_executor', None) is not None,
                  executionProfile=profile,
                  executionStatus='Sage＋原生脸部区域，保留全画面动作并回贴' if profile=='expert-sage-roi-v1' else '专家全画面参考路径')
    if profile=='expert-sage-roi-v1' and project.shot_style!='twoShot':
        result.update(executionAvailable=False,executionStatus='Sage区域模式当前支持双人同框，请切换为同框后重新检查')
    result['planId'] = 'EP-'+digest({'selection': result['planId'], 'snapshot': result['snapshotId']})[:24]
    if operation=='continue':
        from ..services.expert_continuation import freeze_reuse
        result=freeze_reuse(state,project,result)
    if hasattr(state, 'expert_plan_store'):
        state.expert_plan_store.save(result)
    return result


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str = Field(alias='planId', pattern=r'^EP-[a-f0-9]{24}$')
    client_token: str = Field(alias='clientToken', pattern=r'^[A-Za-z0-9_-]{8,128}$')


@router.post('/generate')
async def execute(project_id: str, payload: ExecuteRequest, request: Request):
    state = request.app.state
    project = current_project(state, project_id)
    for job in state.job_manager.list(project_id):
        if job.kind == 'expert.generate' and job.client_token == payload.client_token:
            if job.input_snapshot['expertSelection']['planId'] != payload.plan_id:
                raise HTTPException(409, '请求标识已用于其他计划')
            return {'jobId': job.id}
    plan = state.expert_plan_store.get(project_id, payload.plan_id)
    if plan is None:
        raise HTTPException(404, '范围计划不存在')
    if plan['dependencyHash'] != dependency_hash(project):
        raise HTTPException(409, '范围计划已过期，请重新检查')
    if plan.get('operation')=='continue' and state.expert_version_store.read(project_id)['revision']!=plan['reuseSelectionRevision']:
        raise HTTPException(409,'采用版本已变化，请重新检查续算计划')
    if plan.get('operation')=='continue' and plan['generateWindowCount']==0:
        raise HTTPException(422,'全部片段已采用，请直接合成整期，无需再次生成')
    if getattr(state, 'expert_executor', None) is None:
        raise HTTPException(503, '专家执行器尚未配置，不能提交生成')
    if getattr(state.expert_executor,'profile',None)=='expert-sage-roi-v1' and project.shot_style!='twoShot':
        raise HTTPException(422,'Sage区域模式当前只支持双人同框')
    if plan.get('engineFingerprint') != state.expert_executor.engine_fingerprint:
        raise HTTPException(409, '专家运行配置已改变，请重新检查范围')
    if (plan.get('executionProfile')!=getattr(state.expert_executor,'profile','expert-sage-roi-v1')
            or plan.get('sageTreeHash')!=getattr(state.expert_executor,'sage_tree_hash',None)):
        raise HTTPException(409,'专家提速配置已改变，请重新检查范围')
    snapshot = {'projectSnapshot': project.model_dump(mode='json', by_alias=True),
                'revisionId': project.current_draft_revision, 'dependencyHash': plan['dependencyHash'],
                'expertSelection': plan, 'generationProfile': 'expertMotionReference',
                'expertEngineFingerprint': state.expert_executor.engine_fingerprint,
                'expertProfile':plan['executionProfile'],'sageTreeHash':plan.get('sageTreeHash'),
                'seed': 1, 'resolution': '1280x720', 'fps': 25, 'resumableVideo': True}
    job = state.job_manager.submit(kind='expert.generate', project_id=project_id, queue_class='gpu',
                                   client_token=payload.client_token, input_snapshot=snapshot)
    return {'jobId': job.id}


@router.get('/candidates')
def candidates(project_id: str, request: Request):
    state = request.app.state
    project = current_project(state, project_id)
    identity = dependency_hash(project)
    return {'selection': state.expert_version_store.read(project_id), 'candidates': [
        {'jobId': j.id, 'status': j.status.value, 'stage': j.stage, 'error': j.error,
         'plan': j.input_snapshot['expertSelection'], 'result': j.result,
         'stale': j.input_snapshot['dependencyHash'] != identity}
        for j in state.job_manager.list(project_id) if j.kind == 'expert.generate']}


class AdoptRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(alias='expectedRevision', ge=0)
    checks: list[str]


@router.post('/candidates/{job_id}/adopt')
async def adopt_candidate(project_id: str, job_id: str, payload: AdoptRequest, request: Request):
    state = request.app.state
    project = current_project(state, project_id)
    if set(payload.checks) != {'sync', 'stable', 'blend', 'boundary'}:
        raise HTTPException(422, '请完成口型、稳定性、融合和交接检查')
    job = verified_candidate(state, project_id, job_id, project)
    plan = job.input_snapshot['expertSelection']
    if plan.get('operation')=='continue' and state.expert_version_store.read(project_id)['revision']!=plan['reuseSelectionRevision']:
        raise HTTPException(409,'续算期间采用版本已变化，请重新规划，不能覆盖新选择')
    try:
        return state.expert_version_store.select(project_id, expected_revision=payload.expected_revision,
            snapshot_id=plan['snapshotId'], job_id=job.id, windows=[w['windowId'] for w in plan['windows']], checks=payload.checks)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(alias='expectedRevision', ge=0)
    version: int = Field(ge=1)


@router.post('/restore')
async def restore_version(project_id: str, payload: RestoreRequest, request: Request):
    state = request.app.state
    project = current_project(state, project_id)
    old = next((v for v in state.expert_version_store.read(project_id)['versions'] if v['version'] == payload.version), None)
    if old is None:
        raise HTTPException(404, '历史版本不存在')
    for job_id in set(old['selected'].values()):
        verified_candidate(state, project_id, job_id, project)
    try:
        return state.expert_version_store.select(project_id, expected_revision=payload.expected_revision,
            snapshot_id=old['snapshotId'], restore=payload.version)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.get('/plans')
def saved_plans(project_id: str, request: Request):
    project = request.app.state.project_store.load(project_id)
    if project is None:
        raise HTTPException(404, '工程不存在')
    identity = dependency_hash(project)
    executor = getattr(request.app.state, 'expert_executor', None)
    engine_identity = executor.engine_fingerprint if executor else None
    return [{**plan, 'stale': plan['dependencyHash'] != identity or plan.get('engineFingerprint') != engine_identity
        or plan.get('executionProfile')!=getattr(executor,'profile','expert-sage-roi-v1')
        or plan.get('sageTreeHash')!=getattr(executor,'sage_tree_hash',None)}
            for plan in request.app.state.expert_plan_store.list_for(project_id)]


@router.post('/preview-plan')
def preview_plan(project_id: str, payload: PlanRequest, request: Request):
    return build_plan(project_id, payload, request, 'preview')


@router.post('/continuation-plan')
def continuation_plan(project_id: str,payload: PlanRequest,request: Request):
    if payload.turn_ids is not None or payload.candidate_token is not None:
        raise HTTPException(422,'整期续算由服务端选择全部话轮，无需提供局部范围')
    return build_plan(project_id,payload,request,'continue')


@router.post('/regeneration-plan')
def regeneration_plan(project_id: str, payload: PlanRequest, request: Request):
    if payload.turn_ids is None or payload.candidate_token is None:
        raise HTTPException(422, '局部重做需要明确话轮和候选标识')
    return build_plan(project_id, payload, request, 'regenerate')
