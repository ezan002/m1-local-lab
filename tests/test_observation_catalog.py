"""Catalog inputs are synthetic, temporary and offline; no dataset/DB writes."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from m1_local.historical_observation import CATALOG, HistoryStore, normalize
from m1_local.observation_api import create_observation_app


def catalog_file(tmp_path, **changes):
    entry = {'sample': 'local-case', 'label': 'Locally supplied case', 'source': 'Who&When',
             'version': 'fixture-v1', 'url': 'https://example.test/dataset',
             'license': 'local test only', 'files': ['approved/case.json']}
    entry.update(changes)
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'schema_version': 1, 'samples': [entry]}), encoding='utf-8')
    return path


def source_file(tmp_path, case='one'):
    root = tmp_path / 'sources'
    path = root / 'approved/case.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'question_ID': case, 'question': 'Synthetic fixture',
                               'history': [{'role': 'assistant', 'content': 'fixture message'}]}), encoding='utf-8')
    return root, path


class OfflineRemote:
    def __init__(self):
        self.spans = []

    def read(self, trace_id):
        return copy.deepcopy([s for s in self.spans if s['context']['trace_id'] == trace_id])

    def write(self, spans):
        self.spans.extend(copy.deepcopy(spans))


def test_catalog_import_preview_identity_and_restart(tmp_path):
    from m1_local.historical_observation import load_catalog
    root, path = source_file(tmp_path)
    catalog = load_catalog(catalog_file(tmp_path))
    assert set(CATALOG) < set(catalog) and 'local-case' not in CATALOG
    remote = OfflineRemote()
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'journal', root, remote, catalog=catalog)
    with TestClient(create_observation_app(store=store), base_url='http://127.0.0.1:8000') as client:
        listing = client.get('/api/observation/samples').json()
        assert next(s for s in listing if s['sample'] == 'local-case')['label'] == 'Locally supplied case'
        p = client.get('/api/observation/preview/local-case').json()
        assert p['case_id'] == 'one' and p['original_steps'] == 1
        assert 'operator-declared' in p['version_source']
        headers = {'x-m1-csrf': client.get('/api/observation/session').json()['csrf_token']}
        result = client.post('/api/observation/import', headers=headers, json={'sample': 'local-case', 'import_id': p['import_id']})
        assert result.json()['status'] == 'verified'
        steps = client.get('/api/observation/imports/' + p['import_id'] + '/steps').json()['items']
        assert steps[0]['agent_name'] is None and steps[0]['role'] == 'assistant'
        assert client.post('/api/observation/import', headers=headers, json={'sample': 'local-case', 'import_id': p['import_id'], 'path': str(path)}).status_code == 422
        assert client.post('/api/runs', headers=headers, json={}).status_code == 403
    restarted = HistoryStore(store.endpoint, store.directory, root, remote, catalog=load_catalog(catalog_file(tmp_path)))
    assert restarted.import_sample('local-case', p['import_id'])['status'] == 'verified'
    assert len(remote.spans) == 2
    source_file(tmp_path, 'two')
    assert restarted.preview('local-case')['trace_id'] != p['trace_id']
    with pytest.raises(ValueError, match='changed'):
        restarted.import_sample('local-case', p['import_id'])
    assert len(remote.spans) == 2


@pytest.mark.parametrize('changes', [
    {'sample': 'who-short'}, {'sample': '../escape'}, {'source': 'arbitrary'},
    {'files': ['../secret.json']}, {'files': ['C:/secret.json']},
    {'files': ['folder\\secret.json']}, {'files': ['case.json:stream']},
    {'files': ['a.json', 'b.json']}, {'files': ['script.py']},
    {'url': 'https://user:password@example.test/data'}, {'url': 'file:///secret'},
    {'version': ''}, {'label': 'secret@example.test'}, {'unknown': True},
])
def test_catalog_rejects_unsafe_or_ambiguous_registration(tmp_path, changes):
    from m1_local.historical_observation import load_catalog
    with pytest.raises(ValueError):
        load_catalog(catalog_file(tmp_path, **changes))


def test_catalog_duplicate_fields_and_limit(tmp_path):
    from m1_local.historical_observation import load_catalog
    p = tmp_path / 'catalog.json'
    p.write_text('{"schema_version":1,"schema_version":1,"samples":[]}', encoding='utf-8')
    with pytest.raises(ValueError):
        load_catalog(p)
    p.write_text(' ' * 65537, encoding='utf-8')
    with pytest.raises(ValueError):
        load_catalog(p)


def test_catalog_is_instance_local_and_missing_source_visible(tmp_path):
    from m1_local.historical_observation import load_catalog
    catalog = load_catalog(catalog_file(tmp_path))
    root = tmp_path / 'empty'; root.mkdir()
    store = HistoryStore('http://127.0.0.1:6006', tmp_path / 'journal', root, OfflineRemote(), catalog=catalog)
    with TestClient(create_observation_app(store=store), base_url='http://127.0.0.1:8000') as c:
        assert next(s for s in c.get('/api/observation/samples').json() if s['sample'] == 'local-case')['available'] is False
        assert c.get('/api/observation/preview/local-case').status_code == 404
    assert load_catalog() == CATALOG
    with pytest.raises(ValueError):
        normalize('local-case', root)


def test_registered_symlink_cannot_escape_source_root(tmp_path):
    from m1_local.historical_observation import load_catalog
    root, path = source_file(tmp_path)
    outside = tmp_path / 'outside.json'; outside.write_text('{}', encoding='utf-8')
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f'OS does not permit fixture symlinks: {type(exc).__name__}')
    with pytest.raises(ValueError):
        normalize('local-case', root, catalog=load_catalog(catalog_file(tmp_path)))


def test_resolved_root_boundary_even_for_programmatic_catalog(tmp_path):
    root = tmp_path / 'sources'; root.mkdir()
    (tmp_path / 'outside.json').write_text('{}', encoding='utf-8')
    spec = {**CATALOG['who-short'], 'files': ['../outside.json']}
    with pytest.raises(ValueError):
        normalize('outside', root, catalog={'outside': spec})


def test_traceelephant_registration_uses_explicit_pair_order(tmp_path):
    from m1_local.historical_observation import load_catalog
    root = tmp_path / 'sources'; root.mkdir()
    (root / 'meta.json').write_text(json.dumps({'run_id': 'synthetic-te', 'task_instruction': 'Fixture'}), encoding='utf-8')
    (root / 'steps.json').write_text(json.dumps([{'step_id': 22, 'input': {}, 'output': {}}]), encoding='utf-8')
    catalog = load_catalog(catalog_file(tmp_path, source='TraceElephant', files=['meta.json', 'steps.json']))
    trace = normalize('local-case', root, catalog=catalog)
    assert trace['records'][0]['original_step_id'] == 22
    assert trace['records'][0]['kind'] == 'agent_activity'
    reversed_catalog = load_catalog(catalog_file(tmp_path, source='TraceElephant', files=['steps.json', 'meta.json']))
    with pytest.raises(ValueError):
        normalize('local-case', root, catalog=reversed_catalog)
