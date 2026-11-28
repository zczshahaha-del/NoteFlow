from __future__ import annotations

from pydantic import ValidationError

from app.memory.domain import (
    candidate_review_tags,
    extract_memory_candidates,
    is_single_value_key,
    memory_candidate_status,
    normalize_canonical_key,
)
from app.memory.extraction import candidates_from_llm_payload
from app.memory.planning import memory_read_plan_from_turn_plan
from app.services.turn_planner import plan_from_llm_payload


def _assert(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def _first(text: str):
    candidates = extract_memory_candidates(text, "global")
    _assert(bool(candidates), f"expected candidate for: {text}")
    return candidates[0]


def main():
    name = _first("我叫张成")
    _assert(name.canonical_key == "identity.name", "name should use normalized identity key")
    _assert(memory_candidate_status(name) == "active", "explicit name should become active memory")

    age = _first("我今年24岁了")
    _assert(age.canonical_key == "profile.age", "age should use normalized age key")
    _assert(memory_candidate_status(age) == "active", "age statement should become active memory")

    _assert(
        normalize_canonical_key("personal_info.sleep_habit", "personal_info") == "lifestyle.sleep_schedule",
        "sleep key aliases should normalize to lifestyle.sleep_schedule",
    )
    _assert(
        normalize_canonical_key("career.interview_result", "episode") == "career.interview_history",
        "interview key aliases should normalize to career.interview_history",
    )
    _assert(is_single_value_key("personal_info.sleep_habit"), "sleep schedule should behave as a single-value memory")

    body = _first("我很瘦你知道吗")
    _assert(body.memory_type == "personal_info", "personal body info should be personal_info")
    _assert(memory_candidate_status(body) == "active", "clear personal info should become active memory")

    workflow = _first("我写代码前喜欢先画流程图")
    _assert(workflow.memory_type == "workflow", "workflow habit should keep workflow type")
    _assert(memory_candidate_status(workflow) == "pending", "long-tail workflow habit should become pending candidate")
    _assert("candidate:needs_review" in candidate_review_tags(workflow), "pending candidate should carry review tag")

    skill = _first("我最近在学 Redis")
    _assert(memory_candidate_status(skill) == "pending", "recent learning signal should become pending candidate")

    payload = {
            "candidates": [
                {
                    "memory_type": "personal_info",
                    "layer": "semantic",
                    "canonical_key": "lifestyle.sleep_schedule",
                    "value": "作息较乱，经常熬夜",
                    "content": "用户生活习惯：作息较乱，经常熬夜",
                    "importance": 4,
                    "confidence": 0.88,
                    "source": "user_explicit",
                    "scope": "global",
                    "tags": ["生活习惯", "睡眠"],
                    "subject": "user",
                    "temporal_scope": "durable",
                    "stability": "medium",
                    "operation": "upsert",
                },
                {
                    "memory_type": "identity",
                    "layer": "semantic",
                    "canonical_key": "friend.name",
                    "value": "李雷",
                    "content": "用户朋友叫李雷",
                    "importance": 2,
                    "confidence": 0.93,
                    "source": "user_explicit",
                    "scope": "global",
                    "tags": ["朋友"],
                    "subject": "third_party",
                    "temporal_scope": "durable",
                    "stability": "high",
                    "operation": "upsert",
                },
                {
                    "memory_type": "personal_info",
                    "layer": "semantic",
                    "canonical_key": "mood.today",
                    "value": "今天有点累",
                    "content": "用户今天有点累",
                    "importance": 2,
                    "confidence": 0.91,
                    "source": "user_explicit",
                    "scope": "global",
                    "tags": ["状态"],
                    "subject": "user",
                    "temporal_scope": "temporary",
                    "stability": "low",
                    "operation": "upsert",
                },
                {
                    "memory_type": "episode",
                    "layer": "episodic",
                    "canonical_key": "career.interview_history",
                    "value": "上个月金山面试没有通过，准备了三个月，很可惜",
                    "content": "用户历史经历：上个月金山面试没有通过，准备了三个月，很可惜",
                    "importance": 4,
                    "confidence": 0.9,
                    "source": "user_explicit",
                    "scope": "global",
                    "tags": ["面试经历", "求职"],
                    "subject": "user",
                    "temporal_scope": "durable",
                    "stability": "medium",
                    "operation": "upsert",
                },
                {
                    "memory_type": "preference",
                    "layer": "working",
                    "canonical_key": "scratch.note",
                    "value": "临时草稿状态",
                    "content": "用户临时草稿状态：临时草稿状态",
                    "importance": 2,
                    "confidence": 0.95,
                    "source": "user_explicit",
                    "scope": "global",
                    "tags": ["临时"],
                    "subject": "user",
                    "temporal_scope": "durable",
                    "stability": "low",
                    "operation": "upsert",
                },
            ]
        }
    invalid = payload["candidates"].pop()
    try:
        candidates_from_llm_payload({"candidates": [invalid]})
    except ValidationError:
        pass
    else:
        raise AssertionError("working layer must fail the typed extraction contract")
    llm_candidates = candidates_from_llm_payload(payload)
    sleep, friend, temporary, interview = llm_candidates
    _assert(sleep.canonical_key == "lifestyle.sleep_schedule", "llm extractor should preserve long-tail canonical key")
    _assert(sleep.layer == "semantic", "profile facts should stay in semantic layer")
    _assert("layer:semantic" in sleep.tags, "semantic candidate should carry semantic layer tag")
    _assert(memory_candidate_status(sleep) == "active", "direct sleep schedule statement should become active")
    _assert(memory_candidate_status(friend) == "rejected", "third-party candidate should be rejected by policy")
    _assert(memory_candidate_status(temporary) == "rejected", "temporary candidate should be rejected by policy")
    _assert(interview.memory_type == "episode", "interview history should be episodic memory")
    _assert(interview.layer == "episodic", "episode memory should use episodic layer")
    _assert("layer:episodic" in interview.tags, "episodic candidate should carry episodic layer tag")
    _assert("layer:semantic" not in interview.tags, "episodic candidate should not be tagged semantic")
    _assert(interview.canonical_key == "career.interview_history", "interview history should keep canonical key")
    _assert(memory_candidate_status(interview) == "active", "explicit interview history should become active")

    for query, layers, types, key in [
        ("你还记得我的作息怎么样吗", ["semantic"], ["personal_info"], "lifestyle.sleep_schedule"),
        ("你还记得我金山的面试情况吗", ["episodic", "semantic"], ["episode", "goal"], "career.interview_history"),
    ]:
        turn = plan_from_llm_payload({
            "primary_intent": "memory", "confidence": 0.93,
            "context_sources": ["user_memory"],
            "intent_parameters": {"memory_action": "read", "memory_query": query,
                "memory_layers": layers, "memory_types": types, "memory_key": key},
        }, mode="chat")
        read = memory_read_plan_from_turn_plan(turn, question=query)
        _assert(read.layers == layers, "typed layers must survive executor projection")
        _assert(read.memory_types == types, "typed memory types must survive projection")
        _assert(read.canonical_keys == [key], "typed canonical key must survive projection")
        _assert(read.query == query, "typed retrieval query must survive projection")

    print("memory stage4 checks passed")


if __name__ == "__main__":
    main()
