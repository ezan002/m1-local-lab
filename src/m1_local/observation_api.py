"""Local historical observation, intentionally independent of runtime/model configuration."""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .historical_observation import ROOT, HistoryStore, PhoenixHistory, load_catalog


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sample: str = Field(max_length=80)
    import_id: str = Field(pattern=r'^[0-9a-f]{64}$')


def attach_observation(app: FastAPI, store: HistoryStore, token: str, *, execution_enabled=False):
    app.state.history_store = store

    async def call(fn):
        try:
            return await asyncio.to_thread(fn)
        except FileNotFoundError as exc:
            raise HTTPException(404, '本地样本或导入记录不存在；请检查已获准的本地资料，不会自动下载。') from exc
        except ValueError as exc:
            # Validation errors contain only schema/identity information, never source text.
            raise HTTPException(422, '数据或身份校验未通过；请重新预览。没有自动重试写入。') from exc
        except RuntimeError as exc:
            raise HTTPException(409, '导入目录正在被其他进程使用；请等待该导入结束。') from exc
        except (httpx.HTTPError, OSError) as exc:
            raise HTTPException(503, '本地 Phoenix 或存储不可用；检查地址和服务后重试查询。不要删除导入记录。') from exc

    @app.get('/api/observation/session')
    async def session():
        return {'csrf_token': token, 'mode': 'execution' if execution_enabled else 'observation',
                'execution_enabled': execution_enabled, 'phoenix_url': store.endpoint}

    @app.get('/api/observation/health')
    async def health():
        def check():
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
                response = client.get(store.endpoint + '/healthz')
                response.raise_for_status()
            project = store.remote.project()
            # Phoenix 11.35 compares decoded projectId against raw pathname segments.
            # '=' is safe in a path segment; encoding Base64 padding breaks that lookup.
            return {'app': 'healthy', 'phoenix': 'healthy', 'endpoint': store.endpoint,
                    'project_url': store.endpoint + '/projects/' + quote(project['id'], safe='=') + '/traces' if project else None}
        try:
            return await asyncio.to_thread(check)
        except (httpx.HTTPError, ValueError, KeyError):
            return {'app': 'healthy', 'phoenix': 'unavailable', 'endpoint': store.endpoint,
                    'project_url': None, 'message': '请启动指定的本地 Phoenix；本地已保存内容仍可查看。可达不代表已接收 Trace，请查看每条的核对状态。'}

    @app.get('/api/observation/samples')
    async def samples():
        def available(files):
            try:
                for f in files:
                    path = (store.source_root / f).resolve(strict=True)
                    path.relative_to(store.source_root.resolve(strict=True))
                    if not path.is_file():
                        return False
                return True
            except (OSError, ValueError):
                return False
        return [{'sample': key, 'source': spec['source'], 'label': spec.get('label', key),
                 'available': available(spec['files'])} for key, spec in store.catalog.items()]

    @app.get('/api/observation/preview/{sample}')
    async def preview(sample: str):
        return await call(lambda: store.preview(sample))

    @app.post('/api/observation/import')
    async def import_sample(body: ImportRequest):
        return await call(lambda: store.import_sample(body.sample, body.import_id))

    @app.get('/api/observation/imports')
    async def imports():
        return await call(store.list)

    @app.get('/api/observation/imports/{identity}')
    async def detail(identity: str):
        return await call(lambda: store.summary(store.get(identity)))

    @app.get('/api/observation/imports/{identity}/steps')
    async def steps(identity: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        def read():
            trace = store.get(identity)['trace']
            fields = {'index', 'original_step_id', 'numbering', 'kind', 'agent_name', 'role', 'source_file', 'source_pointer', 'span_id', 'redacted', 'original_time'}
            return {'total': len(trace['records']), 'items': [{k: v for k, v in r.items() if k in fields} for r in trace['records'][offset:offset + limit]]}
        return await call(read)

    @app.get('/api/observation/imports/{identity}/steps/{index}')
    async def step(identity: str, index: int):
        def read():
            records = store.get(identity)['trace']['records']
            if not 0 <= index < len(records):
                raise HTTPException(404, '步骤不存在')
            return records[index]
        return await call(read)

    @app.get('/api/observation/imports/{identity}/metadata')
    async def metadata(identity: str):
        return await call(lambda: store.get(identity)['trace']['source_metadata_redacted'])


def create_observation_app(*, endpoint='http://127.0.0.1:6006', directory=None,
                           source_root=None, port=8000, store=None, catalog_path=None) -> FastAPI:
    """No Settings(), dotenv, Gateway, runtime or automatic recovery is constructed."""
    store = store or HistoryStore(endpoint, Path(directory or ROOT / 'data/observation'),
                                   Path(source_root or ROOT / 'data/external'), catalog=load_catalog(catalog_path))
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if isinstance(store.remote, PhoenixHistory):
            store.remote.client.close()

    app = FastAPI(title='M1 Local Historical Observation', lifespan=lifespan)
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if request.headers.get('host') not in hosts:
            return JSONResponse({'detail': 'Invalid Host'}, status_code=400)
        origin = request.headers.get('origin')
        if origin and origin not in {'http://' + h for h in hosts}:
            return JSONResponse({'detail': 'Invalid Origin'}, status_code=403)
        if request.url.path.startswith('/api/runs') or request.url.path == '/execution':
            return JSONResponse({'detail': '仅可查看历史，无法重新执行。观测模式禁止任务、恢复、Replay 和 Fork。'}, status_code=403)
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.headers.get('x-m1-csrf') != token:
            return JSONResponse({'detail': 'CSRF check failed'}, status_code=403)
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'web/observation.html')

    @app.get('/api/health')
    async def app_health():
        return {'app': {'status': 'healthy'}, 'mode': 'observation', 'execution_enabled': False}

    attach_observation(app, store, token)
    return app
