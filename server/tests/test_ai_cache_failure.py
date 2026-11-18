from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from app import database as db
from app.config import cfg
from app.services import ai, ai_cache
from app.services.prompts import NoteGenerateRequest


class AiCacheFailureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.redis = self.enterContext(patch.object(db, "redis_client", AsyncMock()))
        self.redis.get.return_value = None
        self.enterContext(patch.object(cfg, "AI_CACHE_ENABLED", True))
        self.enterContext(patch.object(cfg, "AI_CACHE_TTL_SECONDS", 60))
        self.enterContext(patch.object(cfg, "DEEPSEEK_API_KEY", "synthetic-key"))

    async def test_nonstream_answer_survives_cache_read_and_write_failures_then_recovers(self) -> None:
        self.redis.get.side_effect = RedisConnectionError("read unavailable")
        self.redis.set.side_effect = RedisConnectionError("write unavailable")
        provider = MagicMock()
        provider.__aenter__.return_value.post = AsyncMock(return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "模型返回的正文"}}]},
        ))
        messages = [{"role": "user", "content": "缓存断开后仍应收到回答"}]
        with patch.object(ai.httpx, "AsyncClient", return_value=provider):
            self.assertEqual(await ai.complete_chat(messages), "模型返回的正文")
            provider.__aenter__.return_value.post.assert_awaited_once()
            self.redis.set.assert_awaited_once()

            self.redis.get.side_effect = None
            self.redis.set.side_effect = None
            self.assertEqual(await ai.complete_chat(messages), "模型返回的正文")
            key, answer = self.redis.set.call_args.args
            self.redis.get.side_effect = lambda requested_key: answer if requested_key == key else None
            self.assertEqual(await ai.complete_chat(messages), "模型返回的正文")
            self.assertEqual(provider.__aenter__.return_value.post.await_count, 2)

    async def test_streams_finish_even_if_the_cache_write_fails(self) -> None:
        self.redis.set.side_effect = RedisConnectionError("write unavailable")

        async def provider_stream(_body):
            yield {"choices": [{"delta": {"content": "答案"}, "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        with patch.object(ai, "_stream_deepseek", provider_stream):
            for stream in (
                ai.stream_chat(ai.ChatRequest(question="请回答")),
                ai.stream_note_generate(NoteGenerateRequest(mode="outline", topic="缓存故障")),
            ):
                chunks = [chunk async for chunk in stream]
                self.assertEqual(ai._delta_text(chunks[0]), "答案")
                self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(self.redis.set.await_count, 2)

    async def test_slow_cache_requests_are_cancelled_at_the_deadline(self) -> None:
        async def slow_reply(*_args, **_kwargs):
            await asyncio.sleep(60)

        self.redis.get.side_effect = slow_reply
        self.redis.set.side_effect = slow_reply
        with patch.object(ai_cache, "REDIS_CACHE_TIMEOUT_SECONDS", 0.01, create=True):
            self.assertIsNone(await asyncio.wait_for(ai_cache.get_cached_ai_text("test"), 1))
            await asyncio.wait_for(ai_cache.set_cached_ai_text("test", "答案"), 1)

    async def test_caller_cancellation_still_propagates(self) -> None:
        self.redis.get.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await ai_cache.get_cached_ai_text("test")
