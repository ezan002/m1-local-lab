"""Zero-model acceptance: unique local stores, real sources read-only, real Chromium.

Child Python sockets and browser requests allow loopback only. No installation,
no dataset download, no Phoenix DB access. Only owned processes are stopped.
"""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def guard_network(log: Path):
    def allowed(host):
        if host in {'localhost', None}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def check(host):
        if not allowed(host):
            with log.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'blocked_host': str(host)}) + '\n')
            raise OSError('offline verifier: non-loopback socket blocked')

    connect, connect_ex, getaddrinfo, sendto = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo, socket.socket.sendto
    def connect_local(self, address):
        check(address[0]); return connect(self, address)
    def connect_ex_local(self, address):
        check(address[0]); return connect_ex(self, address)
    def resolve_local(host, *args, **kwargs):
        check(host); return getaddrinfo(host, *args, **kwargs)
    def sendto_local(self, data, *args):
        check(args[-1][0]); return sendto(self, data, *args)
    socket.socket.connect, socket.socket.connect_ex = connect_local, connect_ex_local
    socket.getaddrinfo, socket.socket.sendto = resolve_local, sendto_local


def child(kind, directory, app_port, phoenix_port):
    guard_network(directory / (kind + '-network.jsonl'))
    if kind == 'phoenix':
        sys.argv = [str(ROOT / 'scripts/serve_phoenix.py')]
        runpy.run_path(sys.argv[0], run_name='__main__')
    else:
        import uvicorn
        from m1_local.observation_api import create_observation_app
        app = create_observation_app(endpoint=f'http://127.0.0.1:{phoenix_port}', directory=directory / 'journal', port=app_port,
                                     catalog_path=directory / 'catalog.json')
        assert not any(m in sys.modules for m in ('m1_local.runtime', 'm1_local.models', 'm1_local.websurfer'))
        uvicorn.run(app, host='127.0.0.1', port=app_port, access_log=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', choices=['app', 'phoenix'])
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--app-port', type=int, default=8000)
    parser.add_argument('--phoenix-port', type=int, default=6006)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.directory, args.app_port, args.phoenix_port); return
    directory = ROOT / 'data/test-output/historical-observation' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    print('EVIDENCE_DIR=' + str(directory), flush=True)
    guard_network(directory / 'verifier-network.jsonl')
    import httpx
    from m1_local.historical_observation import HistoryStore, normalize, load_catalog
    from playwright.sync_api import sync_playwright, expect
    base = f'http://127.0.0.1:{args.app_port}'
    endpoint = f'http://127.0.0.1:{args.phoenix_port}'
    report = {'platform': sys.platform, 'python': sys.version.split()[0], 'directory': str(directory),
              'models': 0, 'websurfer': 0, 'external_tasks': 0, 'screenshots': [], 'samples': [],
              'network_scope': 'verifier/app/Phoenix Python socket connect/connect_ex/getaddrinfo/sendto loopback guard + Chromium context request routing; not an OS firewall or native-code egress proof'}
    # Register an existing, previously acquired real sample; no source copy or download.
    (directory / 'catalog.json').write_text(json.dumps({'schema_version': 1, 'samples': [{
        'sample': 'who-hand', 'label': 'Hand-Crafted historical case', 'source': 'Who&When',
        'version': '59b9fcba1aaed7bbf206b5f4d3c68b8face2f49c',
        'url': 'https://huggingface.co/datasets/Kevin355/Who_and_When',
        'license': 'local research only; separate dataset license not declared',
        'files': ['who-and-when/hand-crafted-1.json']}]}), encoding='utf-8')
    catalog = load_catalog(directory / 'catalog.json')
    files = [ROOT / 'data/external' / f for spec in catalog.values() for f in spec['files']]
    before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'PROGRAMFILES', 'PROGRAMFILES(X86)'}}
    env.update(PHOENIX_HOST='127.0.0.1', PHOENIX_PORT=str(args.phoenix_port), PHOENIX_WORKING_DIR=str(directory / 'phoenix'),
               PHOENIX_ALLOW_EXTERNAL_RESOURCES='false', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', OPENAI_API_KEY='', LANGSMITH_TRACING='false')
    processes, handles = {}, []
    client = httpx.Client(trust_env=False, timeout=30)

    def listening(port):
        with socket.socket() as sock:
            sock.settimeout(.3)
            return sock.connect_ex(('127.0.0.1', port)) == 0

    def start(kind):
        port = args.app_port if kind == 'app' else args.phoenix_port
        if listening(port):
            raise RuntimeError(f'port {port} occupied; no existing process stopped')
        python = ROOT / ('.venv' if kind == 'app' else '.venv-phoenix') / 'Scripts/python.exe'
        log = (directory / (kind + '.log')).open('a', encoding='utf-8'); handles.append(log)
        p = subprocess.Popen([str(python), '-B', str(Path(__file__).resolve()), '--child', kind, '--directory', str(directory),
                              '--app-port', str(args.app_port), '--phoenix-port', str(args.phoenix_port)],
                             cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        processes[kind] = p
        print(f'START {kind} pid={p.pid}', flush=True)
        url = base + '/api/health' if kind == 'app' else endpoint + '/healthz'
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if p.poll() is not None:
                raise RuntimeError(f'{kind} exited {p.returncode}; inspect owned log')
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.5)
        raise RuntimeError(f'{kind} readiness timed out')

    def stop(kind):
        p = processes.pop(kind, None)
        if p and p.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True, timeout=20, check=True)
            else:
                p.terminate()
            p.wait(timeout=20)

    started = time.monotonic()
    try:
        report['ports_before'] = {str(p): listening(p) for p in sorted({4317, 6006, 8000, 8001, args.app_port, args.phoenix_port})}
        start('phoenix'); start('app')
        print('SERVICES_READY', flush=True)
        openapi = client.get(endpoint + '/openapi.json'); openapi.raise_for_status()
        schema = openapi.json()
        report['tested_public_paths'] = ['/healthz', '/v1/projects', '/v1/projects/{project_identifier}', '/v1/projects/{project_identifier}/spans']
        report['span_methods'] = list(schema['paths']['/v1/projects/{project_identifier}/spans'])
        api = client.get(base + '/api/observation/session').json()
        headers = {'x-m1-csrf': api['csrf_token']}
        for suffix in ['', '/history/replay', '/history/fork', '/history/resume', '/history/pause']:
            assert client.post(base + '/api/runs' + suffix, headers=headers, json={}).status_code == 403
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')
            blocked, console, errors, failed, bad_status, requests = [], [], [], [], [], []
            def route(request_route):
                from urllib.parse import urlsplit
                url = request_route.request.url; parts = urlsplit(url)
                if parts.hostname in {'127.0.0.1', 'localhost', '::1'} or parts.scheme in {'data', 'about', 'blob'}:
                    request_route.continue_()
                else:
                    blocked.append(parts.scheme + '://' + str(parts.hostname) + parts.path); request_route.abort()
            context.route('**/*', route)
            def register(page):
                page.on('console', lambda msg: console.append(msg.text[:500]) if msg.type == 'error' else None)
                page.on('pageerror', lambda error: errors.append(str(error)[:500]))
                page.on('requestfailed', lambda req: failed.append({'url': req.url.split('?')[0], 'error': req.failure}))
                page.on('response', lambda resp: bad_status.append({'url': resp.url.split('?')[0], 'status': resp.status}) if resp.status >= 400 else None)
                page.on('request', lambda req: requests.append(req.url.split('?')[0]))
            page = context.new_page(); register(page)
            page.goto(base); page.get_by_text('本地观测模式 · 无需 API Key · 后端已禁止执行').wait_for()
            for sample in catalog:
                page.locator('#sample').select_option(sample)
                page.locator('#preview').click()
                expect(page.locator('#import')).to_be_enabled()
                page.locator('#import').click()
                expect(page.locator('#trace-status')).to_contain_text('已逐项读回核对', timeout=30000)
                # Also verify identity, not just stale text from the previous selection.
                trace = normalize(sample, catalog=catalog)
                if sample in {'who-short', 'traceelephant-7'}:
                    assert trace['trace_id'] == {'who-short': '6a33077cf60d674ab78c4041cd08409a',
                                                'traceelephant-7': 'cb661869f02717318effa413ce2ab9e4'}[sample]
                expect(page.locator('#trace-identity')).to_contain_text(trace['trace_id'])
                expect(page.locator('#imports button[aria-pressed="true"]')).to_have_count(1)
                expect(page.locator('#imports button[aria-pressed="true"]')).to_contain_text(trace['case_id'])
                page.locator('#steps button').first.click()
                page.locator('#step-content details').last.locator('summary').click()
                page.screenshot(path=str(directory / (sample + '-detail.png')), full_page=True)
                report['screenshots'].append(sample + '-detail.png')
                store = HistoryStore(endpoint, directory / 'journal', catalog=catalog)
                record = store.get(trace['import_id']); present, expected = store.verify(record)
                assert len(present) == trace['expected_spans']
                assert len(page.locator('#steps button').all()) == trace['original_steps']
                mappings = [{k: r[k] for k in ('index', 'original_step_id', 'source_file', 'source_pointer', 'kind', 'span_id', 'original_time')} for r in trace['records']]
                report['samples'].append({'sample': sample, 'import_id': trace['import_id'], 'trace_id': trace['trace_id'], 'project': trace['project'],
                    'case_id': trace['case_id'], 'original_steps': trace['original_steps'], 'expected_spans': trace['expected_spans'], 'verified_spans': len(present), 'files': trace['files'], 'mapping': mappings})
                again = client.post(base + '/api/observation/import', headers=headers, json={'sample':sample, 'import_id':trace['import_id']})
                assert again.json()['status'] == 'verified'
                assert len(store.remote.read(trace['trace_id'])) == len(present)
                store.remote.client.close()
                print(f'VERIFIED {sample} steps={trace["original_steps"]} spans={len(present)} duplicate=no', flush=True)
            page.reload(); expect(page.locator('#trace-identity')).to_contain_text(trace['trace_id'])
            # Re-select the previously imported TE trace before testing its direct link.
            page.locator('#imports button').filter(has_text=report['samples'][1]['case_id']).click()
            expect(page.locator('#trace-identity')).to_contain_text(report['samples'][1]['trace_id'])
            expect(page.locator('#imports button[aria-pressed="true"]')).to_contain_text(report['samples'][1]['case_id'])
            assert not any('/api/attribution' in r or '/api/runs' in r for r in requests)
            report['main_browser'] = {'console_errors': list(console), 'page_errors': list(errors), 'request_failures': list(failed), 'unexpected_http_errors': list(bad_status)}
            assert not console and not errors and not failed and not bad_status
            health = client.get(base + '/api/observation/health').json()
            project_url = health['project_url']; report['project_url'] = project_url
            context.on('page', register)
            with page.expect_popup() as popup:
                page.locator('#trace-link').click()
            phoenix_page = popup.value
            phoenix_page.get_by_text('Trace Details', exact=True).wait_for(timeout=30000)
            phoenix_page.get_by_text('Imported history [generated grouping; not execution]', exact=True).first.wait_for(timeout=30000)
            assert report['samples'][1]['trace_id'] in phoenix_page.url
            report['direct_trace_navigation'] = 'PASS: main page opened the selected TraceElephant trace by observed Phoenix route'
            phoenix_page.screenshot(path=str(directory / 'phoenix-te-direct-trace.png'), full_page=True)
            report['screenshots'].append('phoenix-te-direct-trace.png')
            phoenix_page.goto(project_url)
            phoenix_page.get_by_text('m1-imported-history-v1', exact=True).first.wait_for(timeout=30000)
            phoenix_page.wait_for_timeout(3000)
            phoenix_page.screenshot(path=str(directory / 'phoenix-project.png'), full_page=True)
            report['screenshots'].append('phoenix-project.png')
            phoenix_page.get_by_role('button', name='Last 7 Days', exact=True).click()
            phoenix_page.wait_for_timeout(750)
            phoenix_page.screenshot(path=str(directory / 'phoenix-time-menu.png'))
            (directory / 'phoenix-time-accessibility.txt').write_text(phoenix_page.locator('body').aria_snapshot(), encoding='utf-8')
            phoenix_page.get_by_role('option', name='Custom', exact=True).click()
            phoenix_page.wait_for_timeout(750)
            phoenix_page.screenshot(path=str(directory / 'phoenix-custom-time.png'))
            (directory / 'phoenix-custom-accessibility.txt').write_text(phoenix_page.locator('body').aria_snapshot(), encoding='utf-8')
            phoenix_page.get_by_role('button', name='Clear start date and time', exact=True).click()
            phoenix_page.get_by_role('button', name='Clear end date and time', exact=True).click()
            phoenix_page.get_by_role('button', name='Apply', exact=True).click()
            expect(phoenix_page.get_by_text('Imported history [generated grouping; not execution]', exact=True)).to_have_count(len(catalog), timeout=15000)
            phoenix_page.screenshot(path=str(directory / 'phoenix-all-history.png'), full_page=True)
            report['screenshots'].append('phoenix-all-history.png')
            report['phoenix_all_time'] = f'PASS: {len(catalog)} imported traces visible'
            filter_box = phoenix_page.get_by_role('textbox').first
            filter_box.fill('context.trace_id == "' + report['samples'][0]['trace_id'] + '"')
            filter_box.press('Enter')
            expect(phoenix_page.get_by_text('Imported history [generated grouping; not execution]', exact=True)).to_have_count(1, timeout=15000)
            phoenix_page.get_by_text('Imported history [generated grouping; not execution]', exact=True).click()
            phoenix_page.wait_for_timeout(1500)
            phoenix_page.screenshot(path=str(directory / 'phoenix-who-trace.png'), full_page=True)
            report['screenshots'].append('phoenix-who-trace.png')
            report['phoenix_trace_url_observed'] = phoenix_page.url
            report['phoenix_trace_filter'] = 'context.trace_id == "' + report['samples'][0]['trace_id'] + '"'
            # Capture accessible control labels for manual UI follow-up; no HTML parsing.
            report['phoenix_controls'] = phoenix_page.get_by_role('button').all_text_contents()
            report['phoenix_browser'] = {'blocked_external': blocked, 'console_errors': console, 'page_errors': errors, 'request_failures': failed, 'http_errors': bad_status}
            browser.close()
        stop('phoenix')
        assert client.get(base + '/api/observation/health').json()['phoenix'] == 'unavailable'
        assert len(client.get(base + '/api/observation/imports').json()) == len(catalog)
        report['phoenix_unavailable'] = 'PASS: health reports unavailable, persisted historical view remains readable'
        stop('app'); start('phoenix'); start('app')
        for item in report['samples']:
            store = HistoryStore(endpoint, directory / 'journal', catalog=catalog)
            record = store.get(item['import_id']); present, expected = store.verify(record)
            assert len(present) == item['expected_spans']
            response = client.get(base + '/api/observation/imports/' + item['import_id']); response.raise_for_status()
            store.remote.client.close()
        report['restart'] = 'PASS: both services restarted, same journal and public Span data verified'
        report['core_result'] = 'PASS'
        report['result'] = 'PARTIAL' if report['phoenix_browser']['blocked_external'] or report['phoenix_browser']['page_errors'] else 'PASS'
    except Exception as exc:
        report['result'] = 'BLOCKED' if isinstance(exc, PermissionError) or any(s in str(exc) for s in ('Executable doesn', 'EPERM', 'occupied')) else 'FAIL'
        report['failure'] = type(exc).__name__ + ': ' + str(exc)[:800]
        raise
    finally:
        report['cleanup_errors'] = []
        for kind in list(processes):
            try:
                stop(kind)
            except Exception as exc:
                report['cleanup_errors'].append(kind + ': ' + type(exc).__name__)
        for handle in handles:
            handle.close()
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['source_unchanged'] = all(hashlib.sha256(p.read_bytes()).hexdigest() == before[str(p.relative_to(ROOT))] for p in files)
        report['ports_after'] = {str(p): listening(p) for p in sorted({4317, 6006, 8000, 8001, args.app_port, args.phoenix_port})}
        (directory / 'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('RESULT=' + report.get('result', 'UNKNOWN'), flush=True)


if __name__ == '__main__':
    main()
