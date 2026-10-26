"""HTTP-only acceptance of the three batches. Run against an isolated mock storage."""
import io
import time
import uuid
import zipfile
import httpx


def run(base='http://127.0.0.1:8100'):
    with httpx.Client(base_url=base, trust_env=False, timeout=20) as client:
        caps = client.get('/api/providers/capabilities').json()
        assert all(c['mode'] == 'mock' for c in caps.values()), 'Only run against isolated mock services'
        assert 'text/html' in client.get('/').headers['content-type']
        p = client.post('/api/projects', json={'title':'三批优化隔离验收'}).json()
        path = '/api/projects/' + p['id']
        draft = {'sourceKind':'script','content':'A: 时间是 12：30。\nB: 我们先检查声音。\nA: 再检查画面。','durationMode':'auto','approxMinutes':3,'title':p['title']}
        saved = client.patch(path, json={'expectedRevision':p['revision'], 'creationDraft':draft})
        assert saved.status_code == 200, saved.text
        assert client.get(path).json()['creationDraft'] == draft
        print('PASS HTTP creation draft round-trip')

        def wait(job_id):
            for _ in range(250):
                job = client.get('/api/jobs/' + job_id).json()
                if job['status'] == 'succeeded': return job
                assert job['status'] not in {'failed','cancelled'}, job
                time.sleep(.05)
            raise AssertionError('job did not complete')

        script = client.post(path+'/script/generate', json={'sourceInput':{'kind':'script','content':draft['content']},'clientToken':uuid.uuid4().hex})
        assert script.status_code == 200, script.text
        wait(script.json()['jobId'])
        p = client.get(path).json()
        assert p['scriptRevisions'][-1]['turns'][0]['lines'][0]['displayText'] == '时间是 12：30。'
        voice = client.post(path+'/voice/synthesize', json={'clientToken':uuid.uuid4().hex})
        wait(voice.json()['jobId'])
        image = client.post(path+'/visual/generate', json={'clientToken':uuid.uuid4().hex,'prompt':'双人录音棚'})
        wait(image.json()['jobId'])
        sample = client.post(path+'/visual/sample', json={'clientToken':uuid.uuid4().hex})
        result = wait(sample.json()['jobId'])
        assert result['inputSnapshot']['rangeEndSec'] > result['inputSnapshot']['rangeStartSec']
        video = client.post(path+'/render/generate', json={'clientToken':uuid.uuid4().hex})
        result = wait(video.json()['jobId'])
        assert result['result']['simulated'] and not result['result']['artifactIds']
        print('PASS HTTP script import -> voice -> visual -> server-planned sample -> demo render')

        package = client.get(path+'/backup')
        assert package.status_code == 200, package.text
        with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
            assert 'duocast-backup.json' in archive.namelist()
        restored = client.post('/api/projects/import-backup', files={'file':('backup.zip',package.content,'application/zip')})
        assert restored.status_code == 200, restored.text
        assert restored.json()['projectId'] != p['id']
        assert restored.json()['summary']['sourceProjectId'] == p['id']
        assert restored.json()['summary']['recheck']
        print('PASS HTTP backup -> import with a new project ID')
        assert client.get('/api/maintenance').status_code == 200
        plan = client.get(path+'/plan').json()
        assert plan['estimateSeconds'] is None
        assert client.get('/api/diagnostics').status_code == 200
        assert client.get('/api/diagnostics/fast-templates').status_code == 200
        check = client.post('/api/providers/text/test').json()
        assert check['simulated'] and '模拟' in check['message']
        print('PASS HTTP maintenance, impact plan, diagnostics and honest mock checks')
        delivery=client.get(path+'/plan/delivery')
        assert delivery.status_code==200 and delivery.json()['ready'] is False
        assert any(i['code']=='no_output' for i in delivery.json()['issues'])
        invalid=client.post('/api/machine/validate-config',json={'runtimeConfig':{'text_provider':'toapis'}}).json()
        assert invalid['valid'] is False and invalid['errors']
        diagnostic=client.get('/api/diagnostics/package')
        assert diagnostic.status_code==200 and 'attachment' in diagnostic.headers['content-disposition']
        assert 'recentFailures' in diagnostic.json()
        print('PASS HTTP delivery readiness, configuration preflight and downloadable safe diagnostics')
        print('ALL PASS (mock only; no real model or visual quality claim)')


if __name__ == '__main__':
    run()
