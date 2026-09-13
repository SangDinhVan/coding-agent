import unittest
from unittest.mock import patch

from model import llm


class LlmConfigurationTests(unittest.TestCase):
    def test_complete_sets_finite_timeout_without_optional_retry_wrapper(self):
        with patch("model.llm.litellm.completion", return_value="response") as completion:
            self.assertEqual(llm.complete([{"role": "user", "content": "hi"}]), "response")
        kwargs = completion.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 120)
        self.assertNotIn("num_retries", kwargs)

    def test_complete_preserves_explicit_timeout_and_retries(self):
        with patch("model.llm.litellm.completion") as completion:
            llm.complete([], timeout=15, num_retries=0)
        self.assertEqual(completion.call_args.kwargs["timeout"], 15)
        self.assertEqual(completion.call_args.kwargs["num_retries"], 0)


if __name__ == "__main__":
    unittest.main()
