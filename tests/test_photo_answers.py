"""Shop-by-photo answers the question asked and never contradicts its own results."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", "app", ".env"))
from app import worker  # noqa: E402
from app.vision import split_phrase  # noqa: E402


async def _rows(question):
    yield f"[results for: {question}]"


def run(image_query, query, sold=True):
    async def go():
        out = []
        with mock.patch.object(worker, "sql_chain_stream_async", _rows) as _, \
             mock.patch.object(worker, "_brand_buyable", return_value=sold):
            async for chunk in worker._image_stream({"image_query": image_query, "query": query}):
                out.append(chunk.get("token", ""))
        return "".join(out)
    return asyncio.run(go())


class PhotoAnswerTest(unittest.TestCase):
    def test_split_phrase(self):
        self.assertEqual(split_phrase("Adidas boots"), ("Adidas", "boots"))
        self.assertEqual(split_phrase("formal shoes for men"), (None, "formal shoes for men"))
        self.assertEqual(split_phrase("Nike running shoes for women"), ("Nike", "running shoes for women"))

    def test_brand_question_without_a_logo_says_so_and_does_not_guess(self):
        out = run("formal shoes for men", "what brand is this?")
        self.assertIn("can't see a brand logo", out)
        self.assertIn("[results for: formal shoes for men]", out)

    def test_brand_question_with_a_logo_names_it(self):
        self.assertIn("reads **Adidas**", run("Adidas boots", "which brand is this?"))

    def test_unbuyable_brand_is_said_and_dropped_from_the_search(self):
        out = run("Brooks running shoes", "i need similar shoes within 3000", sold=False)
        self.assertIn("No **Brooks** shoes are available to buy right now", out)
        self.assertIn("[results for: running shoes i need similar shoes within 3000]", out)

    def test_opener_no_longer_promises_results(self):
        out = run("Adidas boots", "find this brand")
        self.assertNotIn("Showing shoes similar", out)
        self.assertIn("From your photo I searched for **Adidas boots**", out)


if __name__ == "__main__":
    unittest.main()
