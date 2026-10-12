"""Synthetic fixtures only. No model, network, user database or dataset writes."""
import copy
import json
from pathlib import Path

import pytest

from m1_local.historical_observation import HistoryStore, normalize, spans_for, local_endpoint


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'sources'
    path = root / 'who-and-when/algorithm-generated-1.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'question_ID': 'synthetic-case', 'question': 'synthetic task',
        'history': [{'name': 'A', 'role': 'assistant', 'content': 'Fact ' + 'x' * 12000},
                    {'name': 'B', 'role': 'assistant', 'content': 'api_key=sk-syntheticsecret123 TERMINATE'}]}), encoding='utf-8')
    te = root / 'traceelephant/assistant-bench-task-7'
    te.mkdir(parents=True)
    (te / 'trace_metadata.json').write_text(json.dumps({'run_id': 'synthetic-case', 'task_instruction': 'synthetic task', 'is_correct': False}), encoding='utf-8')
    (te / 'step_records.json').write_text(json.dumps([
        {'step_id': 7, 'agent_name': 'A', 'input': {'messages': [{'role': 'user', 'content': 'safe evidence'}]},
         'output': {'object': 'chat.completion', 'choices': [], 'created': 1700000000, 'usage': {'total_tokens': 17}},
         'tool_logs': [{'id': 't1', 'type': 'function', 'function': {'name': 'read'}}]},
        {'step_id': 12, 'agent_name': 'B', 'input': {}, 'output': {'text': 'no call evidence'}}]), encoding='utf-8')
    return root


class Remote:
    def __init__(self):
        self.spans = []
        self.writes = 0
        self.partial = False

    def read(self, trace_id):
        return copy.deepcopy([s for s in self.spans if s['context']['trace_id'] == trace_id])

    def write(self, spans):
        self.writes += 1
        self.spans.extend(copy.deepcopy(spans[:1] if self.partial else spans))
        if self.partial:
            raise TimeoutError('synthetic timeout')


def test_messages_are_not_llm_calls_and_content_is_not_clipped(source):
    trace = normalize('who-short', source)
    assert trace['original_steps'] == trace['retained_steps'] == 2
    assert trace['excluded_steps'] == 0
    assert all(r['kind'] == 'message' and r['request'] is None for r in trace['records'])
    assert len(trace['records'][0]['historical_message']) > 12000
    assert '[REDACTED]' in trace['records'][1]['historical_message']
    assert 'sk-syntheticsecret123' not in json.dumps(trace)
    for r, s in zip(trace['records'], spans_for(trace)[1:]):
        assert r['source_pointer'] == f"/history/{r['index']}"
        assert r['duration_ms'] is None and r['cost'] is None
        assert s['span_kind'] != 'LLM' and s['status_code'] == 'UNSET'
        assert s['attributes']['historical.time_is_display_only'] is True
        assert s['start_time'].startswith('2000-01-01')


def test_call_evidence_numbering_time_and_labels_are_separate(source):
    trace = normalize('traceelephant-7', source)
    assert trace['task_failed'] is True
    assert [r['kind'] for r in trace['records']] == ['model_call', 'agent_activity']
    assert [r['original_step_id'] for r in trace['records']] == [7, 12]
    spans = spans_for(trace)
    assert len(spans) == 3  # tool declarations do not prove tool execution
    assert spans[1]['start_time'].startswith('2023-11-14')
    assert all(s['status_code'] == 'UNSET' for s in spans)
    assert all(r['parent_relation'].startswith('import-generated') for r in trace['records'])
    assert all(r['checkpoint_id'] is None for r in trace['records'])
    assert trace['trace_id'] != normalize('who-short', source)['trace_id']


def test_dedup_persistence_and_readback_detects_corruption(source, tmp_path):
    remote = Remote()
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'ledger', source, remote)
    preview = store.preview('who-short')
    result = store.import_sample('who-short', preview['import_id'])
    assert result['status'] == 'verified'
    reboot = HistoryStore(store.endpoint, store.directory, source, remote)
    assert reboot.import_sample('who-short', preview['import_id'])['status'] == 'verified'
    assert remote.writes == 1 and len(reboot.list()) == 1
    remote.spans[1]['attributes']['historical.message'] = 'changed'
    with pytest.raises(ValueError, match='mismatch'):
        reboot.import_sample('who-short', preview['import_id'])
    assert remote.writes == 1


def test_partial_ack_never_blindly_resends_and_can_reconcile(source, tmp_path):
    remote = Remote(); remote.partial = True
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'ledger', source, remote)
    p = store.preview('who-short')
    assert store.import_sample('who-short', p['import_id'])['status'] == 'uncertain'
    result = store.import_sample('who-short', p['import_id'])
    assert result['status'] == 'partial' and result['uncertain']
    assert remote.writes == 1
    remote.spans = spans_for(normalize('who-short', source))
    assert store.import_sample('who-short', p['import_id'])['status'] == 'verified'
    assert remote.writes == 1


def test_changed_source_requires_new_preview(source, tmp_path):
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'ledger', source, Remote())
    p = store.preview('who-short')
    file = source / 'who-and-when/algorithm-generated-1.json'
    file.write_text(file.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        store.import_sample('who-short', p['import_id'])


def test_invalid_record_refuses_whole_import_without_silent_drop(source):
    file = source / 'who-and-when/algorithm-generated-1.json'
    data = json.loads(file.read_text(encoding='utf-8'))
    data['history'].append(None)
    file.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='nothing imported'):
        normalize('who-short', source)


def test_secret_identity_refused_and_email_private_key_redacted(source):
    from m1_local.historical_observation import redact
    assert redact('contact fixture@example.test') == 'contact [REDACTED_EMAIL]'
    assert 'fixture-key-body' not in redact('-----BEGIN PRIVATE KEY-----\nfixture-key-body\n-----END PRIVATE KEY-----')
    file = source / 'who-and-when/algorithm-generated-1.json'
    data = json.loads(file.read_text(encoding='utf-8'))
    data['question_ID'] = 'sk-syntheticsecret123'
    file.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='identity'):
        normalize('who-short', source)


def test_unattempted_partial_records_are_completed(source, tmp_path):
    remote = Remote()
    trace = normalize('who-short', source)
    remote.spans = spans_for(trace)[:1]
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'ledger', source, remote)
    result = store.import_sample('who-short', trace['import_id'])
    assert result['status'] == 'verified' and len(remote.spans) == 3
    assert remote.writes == 1


@pytest.mark.parametrize('field,value', [('start_time', '2001-01-01T00:00:00+00:00'), ('status_code', 'ERROR'), ('parent_id', '0' * 16)])
def test_readback_checks_time_status_parent(source, tmp_path, field, value):
    remote = Remote()
    trace = normalize('who-short', source)
    remote.spans = spans_for(trace)
    remote.spans[1][field] = value
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'ledger', source, remote)
    with pytest.raises(ValueError, match='mismatch'):
        store.import_sample('who-short', trace['import_id'])
    assert remote.writes == 0


@pytest.mark.parametrize('endpoint', ['https://127.0.0.1:6006', 'http://example.com:6006', 'http://localhost:6006', 'http://127.0.0.1:6006/path', 'http://u:p@127.0.0.1:6006'])
def test_only_explicit_loopback(endpoint):
    with pytest.raises(ValueError):
        local_endpoint(endpoint)


def test_no_key_mode_has_no_runtime_and_blocks_execution(source, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from m1_local.observation_api import create_observation_app
    monkeypatch.setenv('OPENAI_API_KEY', '')
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'ledger', source, Remote())
    with TestClient(create_observation_app(store=store), base_url='http://127.0.0.1:8000') as client:
        session = client.get('/api/observation/session').json()
        assert session['mode'] == 'observation' and session['execution_enabled'] is False
        headers = {'x-m1-csrf': session['csrf_token']}
        for suffix in ['', '/old/resume', '/old/replay', '/old/fork', '/old/pause']:
            assert client.post('/api/runs' + suffix, headers=headers, json={}).status_code == 403
        assert not hasattr(client.app.state, 'runtime')
        assert client.get('/').status_code == 200
        p = client.get('/api/observation/preview/who-short').json()
        assert client.post('/api/observation/import', json={'sample': 'who-short', 'import_id': p['import_id']}).status_code == 403
        r = client.post('/api/observation/import', headers=headers, json={'sample': 'who-short', 'import_id': p['import_id']})
        assert r.json()['status'] == 'verified'
        assert client.get('/api/observation/imports/' + p['import_id'] + '/steps/0').json()['index'] == 0
        assert client.get('/api/observation/preview/unknown').status_code == 422
        assert client.get('/api/observation/imports/invalid').status_code == 422
        assert client.get('/api/observation/samples', headers={'host': 'evil.test'}).status_code == 400


@pytest.mark.parametrize('project_id', ['UHJvamVjdDox', 'UHJvamVjdDoxNw==', 'UHJvamVjdDoxMDA='])
def test_phoenix_project_link_preserves_base64_padding(source, tmp_path, monkeypatch, project_id):
    """Phoenix 11.35 compares decoded route params with raw pathname segments."""
    from fastapi.testclient import TestClient
    from m1_local.observation_api import create_observation_app
    import m1_local.observation_api as observation_api

    class HealthyClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            assert url == 'http://127.0.0.1:6006/healthz'
            return self

        def raise_for_status(self):
            pass

    remote = Remote()
    remote.project = lambda: {'id': project_id}
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'ledger', source, remote)
    monkeypatch.setattr(observation_api.httpx, 'Client', HealthyClient)
    with TestClient(create_observation_app(store=store), base_url='http://127.0.0.1:8000') as client:
        health = client.get('/api/observation/health').json()
    assert health['phoenix'] == 'healthy'
    assert health['project_url'] == 'http://127.0.0.1:6006/projects/' + project_id + '/traces'
