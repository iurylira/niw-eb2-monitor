import unittest

from scripts.ollama_support import parse_ollama_models, select_qwen_model


class TestOllamaSupport(unittest.TestCase):
    def test_parse_ollama_models_filters_qwen(self):
        sample = (
            "NAME                  ID              SIZE      MODIFIED\n"
            "qwen2.5:7b            845dbda0ea48    4.7 GB    7 months ago\n"
            "llama3.2:latest       a80c4f17acd5    2.0 GB    18 months ago\n"
            "qwen2.5:3b            357c53fb659c    1.9 GB    3 days ago\n"
        )
        self.assertEqual(parse_ollama_models(sample), ["qwen2.5:7b", "qwen2.5:3b"])

    def test_select_qwen_model_prefers_smaller_instruct_model(self):
        sample = (
            "NAME                  ID              SIZE      MODIFIED\n"
            "qwen2.5:3b            357c53fb659c    1.9 GB    3 days ago\n"
            "qwen2.5:7b            845dbda0ea48    4.7 GB    7 months ago\n"
        )
        self.assertEqual(select_qwen_model(sample), "qwen2.5:3b")

    def test_select_qwen_model_prefers_instruct_over_coder(self):
        sample = (
            "NAME                  ID              SIZE      MODIFIED\n"
            "qwen3-coder:30b       06c1097efce0    18 GB     5 months ago\n"
            "qwen2.5:7b            845dbda0ea48    4.7 GB    7 months ago\n"
        )
        self.assertEqual(select_qwen_model(sample), "qwen2.5:7b")

    def test_select_qwen_model_raises_when_missing(self):
        sample = (
            "NAME                  ID              SIZE      MODIFIED\n"
            "llama3.2:latest       a80c4f17acd5    2.0 GB    18 months ago\n"
        )
        with self.assertRaises(RuntimeError):
            select_qwen_model(sample)


if __name__ == "__main__":
    unittest.main()
