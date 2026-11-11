from __future__ import annotations

import unittest
from app.memory.domain import extract_memory_candidates


def _keys(text: str) -> list[str]:
    return [candidate.canonical_key for candidate in extract_memory_candidates(text, "global")]


def _assert(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def main():
    candidates = extract_memory_candidates(
        "我叫张成，今年24岁，最近正在找 Python 后端工作，我喜欢玩率土之滨。",
        "global",
    )
    keys = [candidate.canonical_key for candidate in candidates]
    _assert("identity.name" in keys, "should extract identity.name")
    _assert("profile.age" in keys, "should extract profile.age")
    _assert("career.current_status" in keys, "should extract career.current_status")
    _assert("interest.general" in keys, "should extract interest.general")

    _assert(_keys("我朋友叫张成") == [], "third-party identity must not become user memory")
    _assert(_keys("你还记得我多大了吗") == [], "memory query must not become memory write")
    _assert(_keys("今天我有点累") == [], "temporary state must not become semantic memory")

    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "tests.test_turn_planner.TurnPlannerTest.test_implicit_memory_gate_is_separate_from_primary_intent",
        "tests.test_turn_planner.TurnPlannerTest.test_memory_query_is_typed",
        "tests.test_turn_planner.TurnPlannerTest.test_current_note_summary_is_general_chat_with_context",
    ])
    _assert(unittest.TextTestRunner().run(suite).wasSuccessful(), "current typed planner contracts")

    print("memory stage1 checks passed")


if __name__ == "__main__":
    main()
