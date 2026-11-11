"""Current planner/graph contracts; synthetic model replies, no keyword fallback."""
import unittest

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "tests.test_turn_planner", "tests.test_chat_mode_routing", "tests.test_context_projection",
    ])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
