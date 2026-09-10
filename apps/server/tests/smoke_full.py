import json
import time
import urllib.request

BASE = 'http://127.0.0.1:8100/api'


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_job(job_id, secs=30):
    for _ in range(int(secs / 0.4)):
        s, jj = req('GET', '/jobs/' + job_id)
        if jj['status'] in ('succeeded', 'failed', 'cancelled'):
            return jj
        time.sleep(0.4)
    return jj


# 1. create project
s, proj = req('POST', '/projects', {'title': '冒烟测试'})
print('create:', s, proj['id'], 'rev', proj['revision'])
pid = proj['id']

# 2. generate script (job)
s, job = req('POST', f'/projects/{pid}/script/generate',
             {'clientToken': 'sm-1',
              'sourceInput': {'kind': 'article', 'content': '第一段内容。\n第二段内容。\n第三段内容。'},
              'brief': {}})
print('script job:', s, job.get('jobId'))
jj = wait_job(job['jobId'])
print('script status:', jj['status'])

s, proj = req('GET', f'/projects/{pid}')
print('project: rev', proj['revision'], 'draft', proj['currentDraftRevision'],
      'stages', proj['stageProgress'])
rev_id = proj['currentDraftRevision']

# 3. voice synthesize
s, job = req('POST', f'/projects/{pid}/voice/synthesize',
             {'clientToken': 'sm-2', 'revisionId': rev_id,
              'voiceBindings': {'A': {'id': 'VB-A', 'characterId': 'a1', 'providerProfileId': 'mock-tts'},
                                'B': {'id': 'VB-B', 'characterId': 'b1', 'providerProfileId': 'mock-tts'}}})
print('voice job:', s, job.get('jobId'))
jj = wait_job(job['jobId'], 60)
print('voice status:', jj['status'], '| result:', json.dumps(jj.get('result'), ensure_ascii=False))

s, proj = req('GET', f'/projects/{pid}')
tl = proj.get('audioTimeline')
print('timeline: units', len(tl['units']) if tl else 0,
      'sampleCount', tl['sampleCount'] if tl else 0)

# 4. visual generate
s, job = req('POST', f'/projects/{pid}/visual/generate',
             {'clientToken': 'sm-3', 'aspect': 'landscape'})
jj = wait_job(job['jobId'])
print('visual status:', jj['status'])
s, proj = req('GET', f'/projects/{pid}')
print('variants:', len(proj.get('visualVariants', [])))

# 5. render generate
s, job = req('POST', f'/projects/{pid}/render/generate',
             {'clientToken': 'sm-4', 'revisionId': rev_id, 'resolution': '1920x1080', 'fps': 30})
jj = wait_job(job['jobId'], 60)
print('render status:', jj['status'], '| result:', json.dumps(jj.get('result'), ensure_ascii=False))
s, proj = req('GET', f'/projects/{pid}')
print('outputVersion:', proj.get('outputVersion'), '| stages', proj['stageProgress'])

# 6. artifacts
s, art = req('GET', '/artifacts')
print('artifacts:', len(art.get('artifacts', [])))
