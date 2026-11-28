"""Fault boundaries of the local acceptance harness; no network or application data."""
from __future__ import annotations
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import httpx

spec=importlib.util.spec_from_file_location('local_soak_runner',Path(__file__).resolve().parents[2]/'scripts/run_local_soak.py')
soak_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(soak_module)


class LocalSoakRunnerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        args=SimpleNamespace(report=str(Path(self.folder.name)/'result.json'),duration_seconds=.002,concurrency=2,
            pause_seconds=.001,sample_interval_seconds=.001,write_interval_seconds=0,skip_external=True,base_url='http://127.0.0.1:8080')
        self.runner=soak_module.Soak(args)

    def test_nearest_rank_p95_does_not_fall_below_median(self):
        self.assertEqual(soak_module.stats([2,10])['p95Ms'],10)
        self.assertEqual(soak_module.stats(list(range(1,101)))['p95Ms'],95)
        self.assertIsNone(soak_module.stats([])['p95Ms'])

    async def test_http_failure_is_counted_without_response_or_exception_secrets(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(503,text='synthetic-private-body')),base_url='http://local.invalid') as client:
            with self.assertRaises(soak_module.RequestFailed) as error:
                await self.runner.request(client,'probe','GET','/test')
        self.assertEqual(str(error.exception),'probe');self.assertEqual(self.runner.failures['probe:HTTP_503'],1)
        self.runner.write_report();self.assertNotIn('synthetic-private-body',self.runner.path.read_text())

    async def test_expected_denial_is_not_reported_as_runtime_error(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(404)),base_url='http://local.invalid') as client:
            await self.runner.request(client,'cross_account','GET','/test',expected=404)
        self.assertFalse(self.runner.failures);self.assertEqual(self.runner.statuses['404'],1)

    async def test_transport_failure_message_is_not_written_to_report(self):
        def fail(request):raise httpx.ReadTimeout('synthetic-private-transport-detail',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail),base_url='http://local.invalid') as client:
            with self.assertRaises(soak_module.RequestFailed):await self.runner.request(client,'probe','GET','/test')
        self.runner.write_report();self.assertNotIn('synthetic-private-transport-detail',self.runner.path.read_text())
        self.assertEqual(self.runner.failures['probe:ReadTimeout'],1)

    async def test_expired_cookie_refreshes_and_retries_without_counting_failure(self):
        calls=[]
        def handle(request):
            calls.append(request.url.path)
            if request.url.path=='/api/auth/refresh':
                return httpx.Response(200,headers={'set-cookie':'noteflow_session=synthetic-fresh; Path=/; HttpOnly'},json={'user':{}})
            return httpx.Response(200 if 'synthetic-fresh' in request.headers.get('cookie','') else 401,json={'ok':True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='http://127.0.0.1',cookies={'noteflow_session':'synthetic-expired'}) as client:
            response=await self.runner.request(client,'protected','GET','/protected')
        self.assertEqual(response.status_code,200);self.assertFalse(self.runner.failures)
        self.assertEqual(self.runner.auth_challenges,1);self.assertEqual(self.runner.auth_refreshes,1)
        self.assertEqual(calls,['/protected','/api/auth/refresh','/protected'])
        self.runner.write_report();self.assertNotIn('synthetic-fresh',self.runner.path.read_text())

    async def test_rejected_refresh_cannot_loop_or_report_success(self):
        calls=[]
        def handle(request):calls.append(request.url.path);return httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='http://127.0.0.1') as client:
            with self.assertRaises(soak_module.RequestFailed):await self.runner.request(client,'protected','GET','/protected')
        self.assertEqual(calls,['/protected','/api/auth/refresh'])
        self.assertEqual(self.runner.failures['session_refresh:HTTP_401'],1)

    async def test_worker_index_failure_cannot_silently_pass(self):
        self.runner.fixtures=[{'id':'a','content':'expected','marker':'a','attachment':'att-a','bytes':b'a'},
                              {'id':'b','content':'expected','marker':'b','attachment':'att-b','bytes':b'b'}]
        self.runner.clients=[object(),object()]
        self.runner.request=AsyncMock(return_value=httpx.Response(200,json={'note':{'content':'expected'}}))
        self.runner.index_ready=AsyncMock(side_effect=RuntimeError('synthetic index failure'))
        await self.runner.worker(0,soak_module.time.monotonic()+.005)
        self.assertGreater(self.runner.failures['workflow:RuntimeError'],0)

    async def test_failed_cleanup_prevents_pass_status(self):
        self.runner.prepare=AsyncMock();self.runner.sample_resources=AsyncMock()
        self.runner.cleanup=AsyncMock(side_effect=RuntimeError('synthetic cleanup failure'))
        with patch.object(soak_module,'engine',SimpleNamespace(dispose=AsyncMock())):result=await self.runner.run()
        self.assertEqual(result,1)
        report=json.loads(self.runner.path.read_text());self.assertEqual(report['status'],'incomplete')
        self.assertFalse(report['syntheticAccountsCleaned'])

    async def test_cancellation_runs_cleanup_and_preserves_incomplete_result(self):
        self.runner.prepare=AsyncMock(side_effect=asyncio.CancelledError())
        self.runner.cleanup=AsyncMock()
        with patch.object(soak_module,'engine',SimpleNamespace(dispose=AsyncMock())):
            with self.assertRaises(asyncio.CancelledError):await self.runner.run()
        self.runner.cleanup.assert_awaited_once()
        self.assertEqual(json.loads(self.runner.path.read_text())['status'],'incomplete')


class RealCookieRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_auth_routes_refresh_expired_access_and_reject_revoked_session(self):
        from datetime import datetime,timezone,timedelta
        from fastapi import FastAPI,Depends
        from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
        from app import deps
        from app.routers import auth
        from app.models.db import User,UserSession
        from app.config import cfg
        import jwt
        database=create_async_engine('sqlite+aiosqlite:///:memory:')
        self.addAsyncCleanup(database.dispose)
        async with database.begin() as c:
            await c.run_sync(User.__table__.create);await c.run_sync(UserSession.__table__.create)
        sessions=async_sessionmaker(database,expire_on_commit=False)
        now=datetime.now(timezone.utc)
        async with sessions() as db:
            db.add(User(id='refresh-user',email='refresh@example.invalid',display_name='Synthetic',email_verified_at=now.replace(tzinfo=None)))
            await db.flush();db.add(UserSession(id='refresh-session',user_id='refresh-user',expires_at=(now+timedelta(hours=2)).replace(tzinfo=None)));await db.commit()
        app=FastAPI();app.include_router(auth.router,prefix='/api')
        @app.get('/protected')
        async def protected(user=Depends(deps.get_current_user)):return {'owner':user.id}
        with tempfile.TemporaryDirectory() as folder:
            args=SimpleNamespace(report=str(Path(folder)/'report.json'),duration_seconds=1,concurrency=2,pause_seconds=1,
                sample_interval_seconds=1,write_interval_seconds=300,skip_external=True,base_url='http://127.0.0.1')
            runner=soak_module.Soak(args)
            expired=jwt.encode({'sub':'refresh-user','email':'refresh@example.invalid','displayName':'Synthetic','sid':'refresh-session',
                'iat':now-timedelta(minutes=31),'exp':now-timedelta(seconds=1)},cfg.JWT_SECRET,algorithm='HS256')
            with patch.object(auth,'AsyncSessionLocal',sessions),patch.object(deps,'AsyncSessionLocal',sessions),patch.object(cfg,'AUTH_COOKIE_SECURE',False):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
                    client.cookies.set(cfg.AUTH_COOKIE_NAME,expired,domain='127.0.0.1',path='/')
                    response=await runner.request(client,'protected','GET','/protected')
                    self.assertEqual(response.json()['owner'],'refresh-user');self.assertEqual(runner.auth_refreshes,1)
                    fresh=client.cookies.get(cfg.AUTH_COOKIE_NAME)
                    self.assertGreater(jwt.decode(fresh,cfg.JWT_SECRET,algorithms=['HS256'])['exp'],now.timestamp())
                    async with sessions() as db:
                        row=await db.get(UserSession,'refresh-session');row.revoked_at=now.replace(tzinfo=None);await db.commit()
                    client.cookies.set(cfg.AUTH_COOKIE_NAME,expired,domain='127.0.0.1',path='/')
                    with self.assertRaises(soak_module.RequestFailed):await runner.request(client,'protected','GET','/protected')
                    self.assertEqual(runner.failures['session_refresh:HTTP_401'],1)
