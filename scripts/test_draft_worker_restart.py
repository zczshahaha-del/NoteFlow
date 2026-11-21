"""Exercise real draft workers in isolated processes and a disposable local PostgreSQL schema."""
from __future__ import annotations

import asyncio
import json
import sys
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import cfg
from app.models.db import Base, Note, NoteCategory, NoteDraft, NoteDraftSection, User
from app.services import draft_worker


def isolated_engine(schema: str):
    return create_async_engine(cfg.DATABASE_URL, connect_args={"server_settings": {"search_path": schema}})


async def child(schema: str, lock_key: int, should_hang: bool) -> None:
    engine = isolated_engine(schema)
    draft_worker.engine = engine
    draft_worker.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    draft_worker._DRAFT_WORKER_LOCK = lock_key

    async def generate(draft, section):
        assert section.id == "second", "Committed chapter must not regenerate"
        print(f"claimed {section.id}", flush=True)
        if should_hang:
            await asyncio.Event().wait()
        return "## 第二章\n\n重启后续写的内容"

    draft_worker._generate_section = generate
    await draft_worker.start_draft_worker()
    try:
        await asyncio.Event().wait()
    finally:
        await draft_worker.stop_draft_worker()
        await engine.dispose()


async def test_restart() -> None:
    if cfg.ENVIRONMENT != "development" or cfg.DB_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Run only against a local development PostgreSQL")
    unique = uuid4().hex
    schema = f"nf_restart_{unique}"
    lock_key = int(unique[:12], 16)
    admin = create_async_engine(cfg.DATABASE_URL)
    database = isolated_engine(schema)
    sessions = async_sessionmaker(database, expire_on_commit=False)
    workers = []
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        tables = [cls.__table__ for cls in (User, NoteCategory, Note, NoteDraft, NoteDraftSection)]
        async with database.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=tables)
        async with sessions.begin() as session:
            session.add(User(id="fixture", email="restart@example.invalid", display_name="合成重启测试"))
            await session.flush()
            session.add(NoteDraft(id="draft", user_id="fixture", title="重启测试", topic="重启测试", status="generating",
                                  draft_config={"generationJob": {"status": "queued", "attempts": {"second": 2}}}))
            await session.flush()
            session.add_all([
                NoteDraftSection(id="first", draft_id="draft", user_id="fixture", title="第一章",
                                 sort_order=0, status="confirmed", content="## 第一章\n\n已保存的第一章"),
                NoteDraftSection(id="second", draft_id="draft", user_id="fixture", title="第二章",
                                 sort_order=1, status="outline_only"),
            ])

        async def spawn_worker(mode):
            process = await asyncio.create_subprocess_exec(
                sys.executable, __file__, mode, schema, str(lock_key), stdout=asyncio.subprocess.PIPE,
            )
            workers.append(process)
            return process

        first = await spawn_worker("hang")
        assert await asyncio.wait_for(first.stdout.readline(), timeout=15) == b"claimed second\n"
        async with sessions() as session:
            before = await session.get(NoteDraftSection, "second")
            assert before.status == "generating" and before.retry_count == 2
            previous_key = before.generation_key

        next_worker = await spawn_worker("finish")
        try:
            await asyncio.wait_for(next_worker.stdout.readline(), timeout=3)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("A second worker claimed the live job concurrently")

        first.kill()
        await asyncio.wait_for(first.wait(), timeout=8)
        assert await asyncio.wait_for(next_worker.stdout.readline(), timeout=15) == b"claimed second\n"
        for _ in range(80):
            async with sessions() as session:
                draft = await session.get(NoteDraft, "draft")
                if draft.status == "assembled":
                    first_section = await session.get(NoteDraftSection, "first")
                    second_section = await session.get(NoteDraftSection, "second")
                    assert first_section.status == "confirmed"
                    assert first_section.content == "## 第一章\n\n已保存的第一章"
                    assert second_section.generation_key != previous_key and second_section.retry_count == 2
                    assert "已保存的第一章" in draft.assembled_content and "重启后续写的内容" in draft.assembled_content
                    assert not list(await session.scalars(select(Note)))
                    break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("Draft did not assemble after the worker restart")
    finally:
        for worker in workers:
            if worker.returncode is None:
                worker.kill()
            await worker.wait()
        await database.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await admin.dispose()

    print(json.dumps({"processKilled": True, "exclusiveWorker": True, "completedChapterPreserved": True,
                      "interruptedAttemptRestored": True, "assembledWithoutCreatingNote": True}))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        asyncio.run(test_restart())
    else:
        mode, schema, lock_key = sys.argv[1:]
        asyncio.run(child(schema, int(lock_key), should_hang=mode == "hang"))
