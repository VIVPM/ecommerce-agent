"""Which shown products a results comparison picks; no model or database calls."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
from app.compare import pick_shown  # noqa: E402

LIST = "\n".join(
    f"{i}. {name}: Rs. {price}, Rating: 4.{i} (100 ratings) "
    f"[View Product](https://www.flipkart.com/{slug}/p/itm{i}?pid=PID{i})"
    for i, (name, slug, price) in enumerate([
        ("NIKE Revolution 7", "nike-revolution-7", 2999),
        ("PUMA Softride", "puma-softride", 2499),
        ("Campus Runner", "campus-runner", 999)], 1))
HISTORY = [{"role": "assistant", "content": LIST}]


class PickShownTest(unittest.TestCase):
    def test_numbers_ordinals_and_whole_list(self):
        picks, _ = pick_shown("compare 1 and 3", HISTORY)
        self.assertEqual([p[0] for p in picks], ["PID1", "PID3"])
        picks, _ = pick_shown("is the first one better than the second?", HISTORY)
        self.assertEqual([p[0] for p in picks], ["PID1", "PID2"])
        picks, _ = pick_shown("which of these is best?", HISTORY)
        self.assertEqual(len(picks), 3)

    def test_named_sides(self):
        picks, _ = pick_shown("nike revolution vs puma softride, which is better?", HISTORY)
        self.assertEqual([p[0] for p in picks], ["PID1", "PID2"])

    def test_refuses_instead_of_guessing(self):
        self.assertIn("at least two", pick_shown("compare 2", HISTORY)[1])
        self.assertIn("no #7", pick_shown("compare 1 and 7", HISTORY)[1])
        self.assertIn("at least two products", pick_shown("compare 1 and 2", [])[1])


if __name__ == "__main__":
    unittest.main()
