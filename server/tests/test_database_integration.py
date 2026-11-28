from __future__ import annotations

import asyncio
import importlib.metadata
import os
import unittest
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from sqlalchemy import delete, func, select, text, update

from app.config import cfg
from app.database import AsyncSessionLocal, engine
from app.models.db import (
    AgentRun,
    AgentCheckpoint,
    Base,
    ChatMessage,
    ChatSession,
    Note,
    NoteAttachment,
    NoteChunk,
    NoteDraft,
    NoteEditPreview,
    NoteIndexJob,
    NoteVersion,
    RagIndexState,
    RagEmbedding,
    RagNode,
    User,
    UserMemory,
    IntegrationOutbox,
    MemorySource,
)
from app.repositories.outbox import OutboxRepository
from app.repositories.attachments import AttachmentRepository
from app.repositories.drafts import DraftRepository
from app.repositories.edits import EditRepository
from app.repositories.index_jobs import IndexJobRepository
from app.repositories.memories import MemoryRepository
from app.repositories.notes import NoteRepository
from app.repositories.runs import RunRepository
from app.services.markdown_index import create_index_job, run_index_job
from app.services.note_library import understand_note_query
from app.memory.control import lock_owner
from app.memory.engine import MemoryUnavailable
from app.memory.service import MemoryService
from app.services.run_service import RunService
from app.services import index_worker
from app.services.embeddings import EmbeddingBatchResult, content_hash
from app.deps import CurrentUser
from app.routers.notes import NoteUpdatePayload, update_note
from app.agent.write_runtime import (
    authorize_draft_save,
    initialize_draft_graph,
    initialize_edit_graph,
    resume_draft_generation,
    resume_edit_graph,
)
from app.rag.pipeline.indexer import create_rag_index_job, run_rag_index_job
from app.rag.pipeline.retrieval import retrieve_candidates, vector_retrieve


RUN_DB_TESTS = os.environ.get("NOTEFLOW_RUN_DB_TESTS") == "1"
LANGGRAPH_DURABLE_INTERRUPT = tuple(
    int(part) for part in importlib.metadata.version("langgraph").split(".")[:2]
) >= (1, 0)


async def _database_snapshot() -> dict:
    connection = await asyncpg.connect(
        host=cfg.DB_HOST,
        port=int(cfg.DB_PORT),
        user=cfg.DB_USER,
        password=cfg.DB_PASSWORD,
        database=cfg.DB_NAME,
    )
    try:
        tables = await connection.fetch(
            """
            select table_name from information_schema.tables
            where table_schema = 'public' and table_type = 'BASE TABLE'
            """
        )
        extensions = await connection.fetch("select extname, extversion from pg_extension")
        migration = await connection.fetchval("select version_num from alembic_version")
        vector_index_count = await connection.fetchval(
            "select count(*) from pg_indexes where schemaname = 'public' and indexdef ilike '%hnsw%'"
        )
        return {
            "tables": {row["table_name"] for row in tables},
            "extensions": {row["extname"]: row["extversion"] for row in extensions},
            "migration": migration,
            "vectorIndexCount": vector_index_count,
        }
    finally:
        await connection.close()


async def _exercise_postgres_write_graph_resume() -> dict:
    suffix = uuid.uuid4().hex
    draft_thread = f"db-draft-graph-{suffix}"
    edit_thread = f"db-edit-graph-{suffix}"
    try:
        draft_initialized = await initialize_draft_graph(
            user_id="graph-test-user", thread_id=draft_thread,
            draft_id=f"draft-{suffix}", topic="测试草稿", outline="## 大纲",
        )
        draft_resumed = await resume_draft_generation(thread_id=draft_thread)
        draft_saved = await authorize_draft_save(
            thread_id=draft_thread, assembled_content="## 大纲\n正文",
        )
        edit_initialized = await initialize_edit_graph(
            user_id="graph-test-user", thread_id=edit_thread,
            note_id=f"note-{suffix}", instruction="修改正文",
            preview_id=f"preview-{suffix}", target_type="note",
            section_id="", selected_text="", source_content_hash="abc",
        )
        edit_revised = await resume_edit_graph(
            thread_id=edit_thread, action="revise", payload={"instruction": "再短一点"},
        )
        edit_applied = await resume_edit_graph(thread_id=edit_thread, action="apply")
        return {
            "draft": draft_initialized and draft_resumed and draft_saved,
            "edit": edit_initialized and edit_revised and edit_applied,
        }
    finally:
        from app.agent.langgraph_readonly import postgres_checkpointer
        async with postgres_checkpointer() as saver:
            await saver.adelete_thread(draft_thread)
            await saver.adelete_thread(edit_thread)


async def _exercise_reused_child_with_new_parent() -> tuple[str, str]:
    suffix = uuid.uuid4().hex
    user = User(
        id=f"db-regression-user-{suffix}",
        email=f"db-regression-{suffix}@local.test",
        display_name="Database Regression",
        password_hash="not-used",
    )
    note = Note(
        id=f"db-regression-note-{suffix}",
        user_id=user.id,
        title="Nested section regression",
        content="# Original root\n\nroot v1\n\n## Stable child\n\nchild body",
        tags=["regression"],
    )
    embedding_stats = SimpleNamespace(indexed=0, reused=0, skipped=2, failed=0)

    try:
        async with AsyncSessionLocal() as session:
            session.add_all([user, note])
            await session.flush()
            with patch(
                "app.services.markdown_index.index_note_chunk_embeddings",
                new=AsyncMock(return_value=embedding_stats),
            ):
                first_job = await create_index_job(session, note)
                await run_index_job(session, note, first_job)
                first_status = first_job.status

                # The child is reusable, but its parent is new. This is the exact
                # shape that previously violated note_sections.parent_id_fkey.
                note.content = "# Replacement root\n\nroot v2\n\n## Stable child\n\nchild body"
                second_job = await create_index_job(session, note)
                await run_index_job(session, note, second_job)
                second_status = second_job.status
            await session.commit()
        return first_status, second_status
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()
        await engine.dispose()


async def _exercise_repository_user_isolation() -> dict[str, bool]:
    suffix = uuid.uuid4().hex
    owner = User(
        id=f"repo-owner-{suffix}",
        email=f"repo-owner-{suffix}@local.test",
        display_name="Repository Owner",
        password_hash="not-used",
    )
    other = User(
        id=f"repo-other-{suffix}",
        email=f"repo-other-{suffix}@local.test",
        display_name="Repository Other",
        password_hash="not-used",
    )
    note = Note(
        id=f"repo-note-{suffix}",
        user_id=owner.id,
        title="Private note",
        content="private",
        tags=[],
    )
    memory = UserMemory(
        id=f"repo-memory-{suffix}",
        user_id=owner.id,
        memory_type="preference",
        content="private memory",
        importance=3,
        source="test",
        scope="global",
        tags=[],
        status="active",
    )
    job = NoteIndexJob(
        id=f"repo-job-{suffix}",
        note_id=note.id,
        user_id=owner.id,
        status="pending",
        retry_count=0,
        max_retries=3,
    )
    draft = NoteDraft(
        id=f"repo-draft-{suffix}",
        user_id=owner.id,
        title="Private draft",
        topic="private",
    )
    edit = NoteEditPreview(
        id=f"repo-edit-{suffix}",
        user_id=owner.id,
        note_id=note.id,
        old_content="private",
        new_content="private updated",
        instruction="private edit",
    )
    attachment = NoteAttachment(
        id=f"repo-attachment-{suffix}",
        user_id=owner.id,
        note_id=note.id,
        file_name="private.txt",
        content_type="text/plain",
        size=7,
        sha256="0" * 64,
        storage_key=f"repo-test/{suffix}.txt",
    )
    chat_session = ChatSession(
        id=f"repo-session-{suffix}",
        user_id=owner.id,
        title="Private session",
    )
    run = AgentRun(
        id=f"repo-run-{suffix}",
        session_id=chat_session.id,
        user_id=owner.id,
        intent="general_chat",
        status="running",
        input_text="private run",
    )
    try:
        async with AsyncSessionLocal() as session:
            # NoteIndexJob only carries scalar foreign keys (there is no ORM
            # relationship for SQLAlchemy to infer insert order from), so make
            # the ownership rows durable in the transaction before dependents.
            session.add_all([owner, other])
            await session.flush()
            session.add(note)
            await session.flush()
            session.add_all([memory, draft, chat_session])
            await session.flush()
            session.add_all([job, edit, attachment, run])
            await session.commit()
            note_repo = NoteRepository(session)
            memory_repo = MemoryRepository(session)
            job_repo = IndexJobRepository(session)
            result = {
                "owner_note": await note_repo.get_active(owner.id, note.id) is not None,
                "other_note": await note_repo.get_active(other.id, note.id) is None,
                "owner_memory": await memory_repo.get(owner.id, memory.id) is not None,
                "other_memory": await memory_repo.get(other.id, memory.id) is None,
                "owner_job": await job_repo.get(owner.id, job.id) is not None,
                "other_job": await job_repo.get(other.id, job.id) is None,
                "other_draft": await DraftRepository(session).get(other.id, draft.id) is None,
                "other_edit": await EditRepository(session).get(other.id, edit.id) is None,
                "other_attachment": await AttachmentRepository(session).get(other.id, attachment.id) is None,
                "other_run": await RunRepository(session).get_run(other.id, run.id) is None,
            }

        cross_user_update_rejected = False
        try:
            with patch("app.memory.control.uses_engine", AsyncMock(return_value=False)):
                await MemoryService().update(
                    other.id,
                    memory.id,
                    SimpleNamespace(
                        memoryType=None,
                        content="cross-user overwrite",
                        importance=None,
                        confidence=None,
                        source=None,
                        scope=None,
                        tags=None,
                        status=None,
                        reason="cross-user test",
                    ),
                )
        except LookupError:
            cross_user_update_rejected = True
        result["other_memory_update"] = cross_user_update_rejected
        result["other_run_cancel"] = not await RunService().cancel(other.id, run.id, "cross-user test")

        async with AsyncSessionLocal() as session:
            owner_memory = await MemoryRepository(session).get(owner.id, memory.id)
            owner_run = await RunRepository(session).get_run(owner.id, run.id)
            result["memory_unchanged"] = owner_memory is not None and owner_memory.content == "private memory"
            result["run_unchanged"] = owner_run is not None and owner_run.status == "running"
        return result
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(User).where(User.id.in_([owner.id, other.id])))
            await session.commit()
        await engine.dispose()


async def _exercise_outbox_idempotency_and_recovery() -> dict[str, bool]:
    suffix = uuid.uuid4().hex
    key = f"db-outbox-{suffix}"
    worker = f"worker-{suffix}"
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                repository = OutboxRepository(session)
                first = await repository.enqueue(
                    topic="test.event", aggregate_type="test", aggregate_id=suffix,
                    idempotency_key=key, payload={"value": 1},
                )
                second = await repository.enqueue(
                    topic="test.event", aggregate_type="test", aggregate_id=suffix,
                    idempotency_key=key, payload={"value": 2},
                )
                same_row = first.id == second.id
            async with session.begin():
                claimed = await OutboxRepository(session).claim_one(worker_id=worker, stale_seconds=1)
                claim_ok = claimed is not None and claimed.id == first.id and claimed.attempts == 1
                if claimed:
                    claimed.heartbeat_at = datetime.utcnow() - timedelta(seconds=10)
            async with session.begin():
                recovered = await OutboxRepository(session).claim_one(worker_id=worker + "-recovery", stale_seconds=1)
                recovery_ok = recovered is not None and recovered.id == first.id and recovered.attempts == 2
            return {"same_row": same_row, "claim_ok": claim_ok, "recovery_ok": recovery_ok}
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(IntegrationOutbox).where(IntegrationOutbox.idempotency_key == key))
            await session.commit()
        await engine.dispose()


async def _exercise_rag_incremental_and_permissions() -> dict[str, bool]:
    suffix = uuid.uuid4().hex
    owner = User(
        id=f"rag-v2-owner-{suffix}", email=f"rag-v2-owner-{suffix}@local.test",
        display_name="RAG v2 Owner", password_hash="not-used",
    )
    other = User(
        id=f"rag-v2-other-{suffix}", email=f"rag-v2-other-{suffix}@local.test",
        display_name="RAG v2 Other", password_hash="not-used",
    )
    note = Note(
        id=f"rag-v2-note-{suffix}", user_id=owner.id, title="RAG v2 Architecture",
        content="# Stable\n\nBM25 and vector use RRF.\n\n# Changed\n\nold parser detail", tags=["RAG"],
    )
    private = Note(
        id=f"rag-v2-private-{suffix}", user_id=other.id, title="Private Neptune",
        content="# Secret\n\nBETA_ONLY_NEPTUNE_7429", tags=["private"],
    )
    original_embedding_key = cfg.EMBEDDING_API_KEY
    original_vector_enabled = cfg.RAG_VECTOR_ENABLED
    try:
        cfg.EMBEDDING_API_KEY = ""
        cfg.RAG_VECTOR_ENABLED = False
        async with AsyncSessionLocal() as session:
            session.add_all([owner, other])
            await session.flush()
            session.add_all([note, private])
            await session.flush()
            first_job = await create_rag_index_job(session, note)
            private_job = await create_rag_index_job(session, private)
            await run_rag_index_job(session, note, first_job)
            await run_rag_index_job(session, private, private_job)
            await session.commit()
            first_nodes = (
                await session.execute(
                    select(RagNode).where(RagNode.note_id == note.id, RagNode.active.is_(True))
                )
            ).scalars().all()
            stable_first = next(node.id for node in first_nodes if node.section_path == ["Stable"])
            changed_first = next(node.id for node in first_nodes if node.section_path == ["Changed"])

            note.content = "# Stable\n\nBM25 and vector use RRF.\n\n# Changed\n\nnew parser detail"
            second_job = await create_rag_index_job(session, note)
            await run_rag_index_job(session, note, second_job)
            await session.commit()
            second_nodes = (
                await session.execute(
                    select(RagNode).where(RagNode.note_id == note.id, RagNode.active.is_(True))
                )
            ).scalars().all()
            stable_second = next(node.id for node in second_nodes if node.section_path == ["Stable"])
            changed_second = next(node.id for node in second_nodes if node.section_path == ["Changed"])
            old_changed = await session.get(RagNode, changed_first)
            state_before_stale = await session.get(RagIndexState, note.id)
            indexed_source = state_before_stale.source_version

            owner_embedding = await session.scalar(
                select(RagEmbedding).where(RagEmbedding.node_id == stable_second)
            )
            private_node = await session.scalar(
                select(RagNode).where(RagNode.note_id == private.id, RagNode.active.is_(True))
            )
            private_embedding = await session.scalar(
                select(RagEmbedding).where(RagEmbedding.node_id == private_node.id)
            )
            owner_embedding.status = "indexed"
            owner_embedding.embedding = [0.01] * cfg.EMBEDDING_DIMENSIONS
            private_embedding.status = "indexed"
            private_embedding.embedding = [0.01] * cfg.EMBEDDING_DIMENSIONS

            stale_job = await create_rag_index_job(session, note, force=True)
            note.content += "\n\nconcurrent update"
            await run_rag_index_job(session, note, stale_job)
            await session.commit()
            state_after_stale = await session.get(RagIndexState, note.id)

        owner_results = await retrieve_candidates(owner.id, "BM25 RRF", limit=8)
        probe_results = await retrieve_candidates(owner.id, "BETA_ONLY_NEPTUNE_7429", limit=8)
        query_plan = understand_note_query("RAG")
        fake_embedding_result = SimpleNamespace(
            embeddings=[[0.01] * cfg.EMBEDDING_DIMENSIONS],
            provider=cfg.EMBEDDING_PROVIDER,
            model=cfg.EMBEDDING_MODEL,
            dimensions=cfg.EMBEDDING_DIMENSIONS,
        )
        with patch("app.rag.pipeline.retrieval.embedding_enabled", return_value=True), patch(
            "app.rag.pipeline.retrieval.embed_texts", new=AsyncMock(return_value=fake_embedding_result)
        ):
            vector_results = await vector_retrieve(owner.id, query_plan, limit=8)
        return {
            "jobs_succeeded": first_job.status == private_job.status == second_job.status == "success",
            "unchanged_reused": stable_first == stable_second,
            "changed_rebuilt": changed_first != changed_second and old_changed is not None and not old_changed.active,
            "owner_found": any(candidate.note_id == note.id for candidate in owner_results.candidates),
            "cross_user_filtered": all(candidate.note_id != private.id for candidate in probe_results.candidates),
            "vector_permission_filtered": bool(vector_results) and all(candidate.note_id != private.id for candidate in vector_results),
            "stale_cancelled": stale_job.status == "cancelled" and stale_job.error_code == "STALE_SOURCE_VERSION",
            "stale_did_not_move_pointer": state_after_stale.source_version == indexed_source,
        }
    finally:
        cfg.EMBEDDING_API_KEY = original_embedding_key
        cfg.RAG_VECTOR_ENABLED = original_vector_enabled
        async with AsyncSessionLocal() as session:
            await session.execute(delete(User).where(User.id.in_([owner.id, other.id])))
            await session.commit()
        await engine.dispose()


async def _exercise_long_note_selection() -> None:
    from app.agent.langgraph_readonly import _default_rag_search, new_readonly_state

    suffix = uuid.uuid4().hex
    owner_id = f"selection-owner-{suffix}"
    owner = User(id=owner_id, email=f"{owner_id}@local.test", display_name="选区验收")
    long_text = (
        "## 开头\n\n这是我选中的第一段。\n\n"
        + "\n\n".join(f"## 日常记录 {i}\n\n" + "这里是与校验口令无关的背景。 " * 18 for i in range(100))
        + "\n\n## 结尾\n\n远处校验词 NEPTUNE_7429 的值是松果。"
    )
    note = Note(id=f"selection-note-{suffix}", user_id=owner_id, title="合成长笔记", content=long_text)
    other = Note(
        id=f"selection-other-{suffix}", user_id=owner_id, title="同账号另一篇笔记",
        content="远处校验词 NEPTUNE_7429 的值是 WRONG_OTHER_NOTE。",
    )
    try:
        with patch.object(cfg, "EMBEDDING_API_KEY", ""), patch.object(cfg, "RAG_VECTOR_ENABLED", False), patch.object(
            cfg, "RAG_BM25_ENABLED", True,
        ), patch.object(cfg, "RAG_RERANK_ENABLED", False):
            async with AsyncSessionLocal() as session:
                session.add(owner)
                await session.flush()
                session.add_all([note, other])
                await session.flush()
                for fixture in (note, other):
                    job = await create_rag_index_job(session, fixture)
                    await run_rag_index_job(session, fixture, job)
                    assert job.status == "success"
                await session.commit()

            state = new_readonly_state(
                user_id=owner_id, question="这一段里的 NEPTUNE_7429 是什么？", mode="chat",
                page_state={
                    "contextScope": "selection", "currentNoteId": note.id,
                    "selectedText": "这是我选中的第一段。", "selectionContext": {"heading": "开头"},
                    "dirty": True, "unsavedContent": f"{long_text}\nUNSAVED_EDITOR_MARKER",
                },
            )
            local = await _default_rag_search(state)
            assert local["context_mode"] == "selection" and not local["sources"]
            assert "松果" not in local["context_text"]
            for page_state in (
                state["page_state"],
                {
                    "contextScope": "current_note", "currentNoteId": note.id,
                    "dirty": True, "unsavedContent": "UNSAVED_EDITOR_MARKER",
                },
            ):
                expanded = await _default_rag_search({
                    **state, "page_state": page_state, "question": "请结合全文说明 NEPTUNE_7429 的值",
                })
                assert expanded["context_mode"] == "note_excerpt" and 1 <= len(expanded["sources"]) <= 3
                assert {source["noteId"] for source in expanded["sources"]} == {note.id}
                assert "松果" in expanded["context_text"]
                assert "WRONG_OTHER_NOTE" not in expanded["context_text"]
                assert "UNSAVED_EDITOR_MARKER" not in expanded["context_text"]
                assert len(expanded["context_text"]) < 3500
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(User).where(User.id == owner_id))
            await session.commit()
        await engine.dispose()


async def _exercise_concurrent_draft_save() -> None:
    import httpx
    from app.deps import CurrentUser, get_current_user
    from app.main import app
    from app.services import draft_save
    from sqlalchemy.ext.asyncio import AsyncSession

    owner_id = f"draft-save-check-{uuid.uuid4().hex}"
    draft_id = f"{owner_id}-draft"
    chat_id = f"{owner_id}-chat"
    run_id = f"{owner_id}-run"
    checkpoint_id = f"{owner_id}-checkpoint"
    user = CurrentUser(id=owner_id, email=f"{owner_id}@local.test", display_name="草稿保存验收")

    async def enqueue_for_test(session, note):
        job = await original_enqueue(session, note)
        # Keep the live background worker from consuming synthetic jobs.
        await session.execute(update(NoteIndexJob).where(NoteIndexJob.note_id == note.id).values(
            next_attempt_at=datetime.utcnow() + timedelta(days=1),
        ))
        return job

    original_enqueue = draft_save.index_note_now
    try:
        async with AsyncSessionLocal() as session:
            session.add(User(id=user.id, email=user.email, display_name=user.display_name))
            await session.flush()
            session.add(NoteDraft(
                id=draft_id, user_id=user.id, title="待保存", topic="合成草稿",
                status="assembled", assembled_content="## 章节\n\n合成正文",
                graph_thread_id=f"{owner_id}-obsolete-graph",
            ))
            session.add(ChatSession(id=chat_id, user_id=user.id))
            await session.flush()
            session.add(AgentRun(id=run_id, session_id=chat_id, user_id=user.id, intent="note_draft_create"))
            await session.flush()
            session.add(AgentCheckpoint(
                id=checkpoint_id, session_id=chat_id, run_id=run_id, user_id=user.id,
                status="waiting_user_confirm", checkpoint_type="draft_workspace", payload={"draftId": draft_id},
            ))
            await session.commit()

        with patch.dict(app.dependency_overrides, {get_current_user: lambda: user}), patch.object(
            draft_save, "index_note_now", enqueue_for_test,
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
                url = f"/api/note-drafts/{draft_id}/save-to-notes"
                denied = await client.post(url, json={"confirm": False})
                assert denied.status_code == 400

                async def fail_commit(session):
                    await session.flush()
                    raise RuntimeError("interrupted before commit")

                with patch.object(AsyncSession, "commit", fail_commit):
                    failed = await client.post(url, json={"confirm": True})
                assert failed.status_code == 500
                async with AsyncSessionLocal() as session:
                    assert (await session.get(NoteDraft, draft_id)).saved_note_id is None
                    assert (await session.get(AgentCheckpoint, checkpoint_id)).status == "waiting_user_confirm"
                    assert not await session.scalar(select(Note.id).where(Note.user_id == user.id))

                results = await asyncio.gather(*[
                    client.post(url, json={"confirm": True, "title": "正式笔记.md", "categoryId": None})
                    for _ in range(2)
                ])
        assert all(response.status_code == 200 for response in results)
        first, second = (response.json() for response in results)
        assert sum(bool(item.get("idempotentReplay")) for item in (first, second)) == 1
        assert first["note"]["id"] == second["note"]["id"]
        assert first["note"]["title"] == "正式笔记"
        assert first["draft"]["savedNoteId"] == first["note"]["id"]

        async with AsyncSessionLocal() as session:
            notes = list((await session.scalars(select(Note).where(Note.user_id == user.id))).all())
            versions = list((await session.scalars(select(NoteVersion).where(NoteVersion.user_id == user.id))).all())
            jobs = list((await session.scalars(select(NoteIndexJob).where(NoteIndexJob.user_id == user.id))).all())
            checkpoint = await session.get(AgentCheckpoint, checkpoint_id)
            assert checkpoint.status == "resolved" and checkpoint.payload["noteId"] == first["note"]["id"]
            assert len(notes) == len(versions) == 1
            assert notes[0].content == versions[0].content == "## 章节\n\n合成正文"
            assert len(jobs) == 2 and {bool(job.graph_version) for job in jobs} == {False, True}
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(User).where(User.id == owner_id))
            await session.commit()
        await engine.dispose()


@unittest.skipUnless(RUN_DB_TESTS, "set NOTEFLOW_RUN_DB_TESTS=1 to run real PostgreSQL integration checks")
class DatabaseIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = asyncio.run(_database_snapshot())

    def test_memory_lock_contention_is_bounded_and_cancellable(self) -> None:
        async def exercise() -> None:
            owner = uuid.uuid4().hex
            try:
                async with AsyncSessionLocal() as holder:
                    await lock_owner(holder, owner)
                    async with AsyncSessionLocal() as waiter:
                        with self.assertRaises(MemoryUnavailable):
                            await asyncio.wait_for(lock_owner(waiter, owner), timeout=6)
                    async with AsyncSessionLocal() as waiter:
                        pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
                        task = asyncio.create_task(lock_owner(waiter, owner))
                        try:
                            await asyncio.sleep(0.3)
                            self.assertFalse(task.done())
                            pending = await holder.scalar(text(
                                "SELECT count(*) FROM pg_locks WHERE pid = :pid AND locktype = 'advisory'",
                            ), {"pid": pid})
                            self.assertEqual(pending, 0)
                        finally:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                async with AsyncSessionLocal() as retry:
                    await asyncio.wait_for(lock_owner(retry, owner), timeout=1)
            finally:
                await engine.dispose()

        asyncio.run(exercise())

    def test_chat_completion_queues_learning_without_waiting_for_memory_lock(self) -> None:
        from app.schemas.agent import AgentChatPayload
        from app.services.agent_runtime import finish_agent_run, start_agent_run

        async def exercise() -> None:
            owner = uuid.uuid4().hex
            async with AsyncSessionLocal() as session:
                session.add(User(id=owner, email=f"{owner}@example.invalid", display_name="Chat test"))
                await session.commit()
            try:
                first = await start_agent_run(AgentChatPayload(question="我是硕士"), owner, "general_chat")
                stopped = await start_agent_run(AgentChatPayload(question="我每天写笔记"), owner, "general_chat")
                retry = await start_agent_run(AgentChatPayload(question="我是硕士", retryAnswerOnly=True,
                    retryOfRunId=first["run_id"]), owner, "general_chat")
                await RunService().cancel(owner, stopped["run_id"], "test")
                async with AsyncSessionLocal() as holder:
                    await lock_owner(holder, owner)
                    for run, expected in ((first, True), (stopped, False), (retry, True)):
                        committed = await asyncio.wait_for(finish_agent_run(
                            user_id=owner, session_id=run["session_id"], run_id=run["run_id"],
                            status="completed", answer="收到", learn_from_chat=True,
                        ), timeout=2)
                        self.assertEqual(committed, expected)
                    async with AsyncSessionLocal() as session:
                        queued = (await session.scalars(select(MemorySource).where(MemorySource.user_id == owner))).all()
                        self.assertEqual([row.metadata_json["runId"] for row in queued], [first["run_id"]])
                        reply = await session.scalar(select(ChatMessage).where(
                            ChatMessage.run_id == first["run_id"], ChatMessage.role == "assistant"))
                        self.assertEqual(reply.text, "收到")
            finally:
                async with AsyncSessionLocal() as session:
                    await session.execute(delete(IntegrationOutbox).where(IntegrationOutbox.user_id == owner))
                    await session.execute(delete(User).where(User.id == owner))
                    await session.commit()
                await engine.dispose()

        with patch.object(cfg, "MEMORY_ENGINE", "hindsight"):
            asyncio.run(exercise())

    def test_selection_only_expands_to_the_attached_long_note(self) -> None:
        asyncio.run(_exercise_long_note_selection())

    def test_draft_save_concurrency_returns_one_note_and_one_version(self) -> None:
        asyncio.run(_exercise_concurrent_draft_save())

    def test_background_index_releases_connection_and_fences_stale_results(self) -> None:
        async def exercise(rag: bool) -> None:
            suffix = uuid.uuid4().hex
            owner = CurrentUser(id=f"index-{suffix}", email=f"{suffix}@example.invalid", display_name="Index test")
            note_id = f"note-{suffix}"
            create_job = create_rag_index_job if rag else create_index_job
            started = asyncio.Event()
            release = asyncio.Event()
            checked_out = []

            async def embed(texts):
                checked_out.append(engine.pool.checkedout())
                started.set()
                await asyncio.wait_for(release.wait(), 5)
                return EmbeddingBatchResult(
                    [[0.01] * cfg.EMBEDDING_DIMENSIONS for _ in texts],
                    cfg.EMBEDDING_PROVIDER, cfg.EMBEDDING_MODEL, cfg.EMBEDDING_DIMENSIONS,
                )

            async def claim(session, job):
                job.status = "running"
                job.claim_owner = index_worker._worker_id
                job.claimed_at = job.heartbeat_at = datetime.utcnow()
                await session.flush()
                return job.id

            worker_task = None
            try:
                async with AsyncSessionLocal() as session, session.begin():
                    session.add(User(id=owner.id, email=owner.email, display_name=owner.display_name))
                    await session.flush()
                    note = Note(id=note_id, user_id=owner.id, title="隔离索引", content="## 测试\n\n旧正文")
                    session.add(note)
                    await session.flush()
                    original = await claim(session, await create_job(session, note))
                with (
                    patch.object(cfg, "EMBEDDING_PROVIDER", "test"),
                    patch.object(cfg, "EMBEDDING_API_KEY", "synthetic"),
                    patch("app.services.embedding_index.embed_texts", new=embed),
                ):
                    worker_task = asyncio.create_task(index_worker._run_claimed(original))
                    await asyncio.wait_for(started.wait(), 5)
                    async with AsyncSessionLocal() as session, session.begin():
                        # A second worker could take this lock while the model is waiting.
                        await session.get(NoteIndexJob, original, with_for_update={"nowait": True})
                    new_body = "## 测试\n\n新正文"
                    await asyncio.wait_for(
                        update_note(note_id, NoteUpdatePayload(content=new_body), owner), 5,
                    )
                    release.set()
                    self.assertTrue(await worker_task)
                    async with AsyncSessionLocal() as session, session.begin():
                        old = await session.get(NoteIndexJob, original)
                        self.assertEqual((old.status, old.error_code), ("cancelled", "STALE_SOURCE_VERSION"))
                        condition = NoteIndexJob.graph_version == "rag-v2" if rag else NoteIndexJob.graph_version.is_(None)
                        fresh = await session.scalar(
                            select(NoteIndexJob).where(
                                NoteIndexJob.note_id == note_id, NoteIndexJob.source_version == content_hash(new_body), condition,
                            )
                        )
                        latest = await claim(session, fresh)
                    self.assertTrue(await index_worker._run_claimed(latest))
                self.assertEqual(checked_out, [0, 0])
                async with AsyncSessionLocal() as session:
                    job = await session.get(NoteIndexJob, latest)
                    self.assertEqual(job.status, "success")
                    if rag:
                        state = await session.get(RagIndexState, note_id)
                        self.assertEqual(state.source_version, content_hash(new_body))
                        parts = await session.scalars(select(RagNode.content).where(RagNode.note_id == note_id, RagNode.active.is_(True)))
                    else:
                        note = await session.get(Note, note_id)
                        self.assertEqual(note.index_version, content_hash(new_body))
                        parts = await session.scalars(select(NoteChunk.content).where(NoteChunk.note_id == note_id))
                    joined = "\n".join(parts)
                    self.assertIn("新正文", joined)
                    self.assertNotIn("旧正文", joined)

                failing_body = "## 测试\n\n等待索引的新版本"
                await update_note(note_id, NoteUpdatePayload(content=failing_body), owner)
                async with AsyncSessionLocal() as session, session.begin():
                    fresh = await session.scalar(
                        select(NoteIndexJob).where(
                            NoteIndexJob.note_id == note_id, NoteIndexJob.source_version == content_hash(failing_body), condition,
                        )
                    )
                    failing = await claim(session, fresh)
                with (
                    patch.object(cfg, "EMBEDDING_PROVIDER", "test"),
                    patch.object(cfg, "EMBEDDING_API_KEY", "synthetic"),
                    patch.object(cfg, "EMBEDDING_FAIL_INDEX_ON_ERROR", True),
                    patch("app.services.embedding_index.embed_texts", new=AsyncMock(side_effect=TimeoutError)),
                ):
                    self.assertTrue(await index_worker._run_claimed(failing))
                async with AsyncSessionLocal() as session:
                    retry = await session.get(NoteIndexJob, failing)
                    self.assertEqual((retry.status, retry.retry_count, retry.error_code), ("pending", 1, "TimeoutError"))
                    self.assertIsNotNone(retry.next_attempt_at)
                    if rag:
                        state = await session.get(RagIndexState, note_id)
                        self.assertEqual((state.status, state.source_version), ("indexed", content_hash(new_body)))
                    else:
                        note = await session.get(Note, note_id)
                        self.assertEqual(note.index_version, content_hash(new_body))
            finally:
                if worker_task is not None and not worker_task.done():
                    worker_task.cancel()
                    await asyncio.gather(worker_task, return_exceptions=True)
                async with AsyncSessionLocal() as session, session.begin():
                    await session.execute(delete(User).where(User.id == owner.id))
                await engine.dispose()

        for rag in (False, True):
            with self.subTest(rag=rag):
                asyncio.run(exercise(rag))

    def test_runtime_tables_match_sqlalchemy_metadata(self) -> None:
        library_owned = {
            "checkpoint_migrations",
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "mem0migrations",
            cfg.MEM0_COLLECTION_NAME,
        }
        self.assertEqual(self.snapshot["tables"] - {"alembic_version"} - library_owned, set(Base.metadata.tables))

    def test_pgvector_and_hnsw_are_available(self) -> None:
        self.assertIn("vector", self.snapshot["extensions"])
        self.assertGreaterEqual(self.snapshot["vectorIndexCount"], 1)

    def test_database_is_at_current_alembic_revision(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from pathlib import Path
        configuration = Config()
        configuration.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        self.assertEqual(self.snapshot["migration"], ScriptDirectory.from_config(configuration).get_current_head())

    @unittest.skipUnless(LANGGRAPH_DURABLE_INTERRUPT, "requires pinned langgraph >= 1.0")
    def test_postgres_write_graphs_interrupt_and_resume(self) -> None:
        result = asyncio.run(_exercise_postgres_write_graph_resume())
        self.assertTrue(result["draft"])
        self.assertTrue(result["edit"])

    def test_outbox_is_idempotent_and_recovers_stale_claims(self) -> None:
        result = asyncio.run(_exercise_outbox_idempotency_and_recovery())
        self.assertTrue(result["same_row"])
        self.assertTrue(result["claim_ok"])
        self.assertTrue(result["recovery_ok"])

    def test_reindex_allows_reused_child_under_new_parent(self) -> None:
        first_status, second_status = asyncio.run(_exercise_reused_child_with_new_parent())
        self.assertEqual(first_status, "success")
        self.assertEqual(second_status, "success")

    def test_rag_incremental_index_fencing_and_permissions(self) -> None:
        result = asyncio.run(_exercise_rag_incremental_and_permissions())
        self.assertTrue(result["jobs_succeeded"])
        self.assertTrue(result["unchanged_reused"])
        self.assertTrue(result["changed_rebuilt"])
        self.assertTrue(result["owner_found"])
        self.assertTrue(result["cross_user_filtered"])
        self.assertTrue(result["vector_permission_filtered"])
        self.assertTrue(result["stale_cancelled"])
        self.assertTrue(result["stale_did_not_move_pointer"])

    def test_owned_repositories_and_services_reject_cross_user_access(self) -> None:
        result = asyncio.run(_exercise_repository_user_isolation())
        self.assertTrue(result["owner_note"])
        self.assertTrue(result["owner_memory"])
        self.assertTrue(result["owner_job"])
        self.assertTrue(result["other_note"])
        self.assertTrue(result["other_memory"])
        self.assertTrue(result["other_job"])
        self.assertTrue(result["other_draft"])
        self.assertTrue(result["other_edit"])
        self.assertTrue(result["other_attachment"])
        self.assertTrue(result["other_run"])
        self.assertTrue(result["other_memory_update"])
        self.assertTrue(result["other_run_cancel"])
        self.assertTrue(result["memory_unchanged"])
        self.assertTrue(result["run_unchanged"])


if __name__ == "__main__":
    unittest.main()
