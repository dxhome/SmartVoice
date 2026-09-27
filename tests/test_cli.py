from __future__ import annotations

import unittest

from smartvoice.__main__ import _format_model_list


class ModelListOutputTests(unittest.TestCase):
    def test_model_list_groups_by_install_state_then_task_compactly(self):
        output = _format_model_list([
            {
                "id": "sensevoice-small-local",
                "name": "SenseVoice Small INT8",
                "task": "transcription",
                "languages": ["zh", "en"],
                "backend": "sherpa-onnx",
                "installed": True,
            },
            {
                "id": "melo-tts-zh-en-local",
                "name": "Melo TTS",
                "task": "speech",
                "languages": ["zh", "en"],
                "backend": "sherpa-onnx",
                "installed": False,
            },
            {
                "id": "paraformer-zh-local",
                "name": "Paraformer Chinese",
                "task": "transcription",
                "languages": ["zh"],
                "backend": "funasr",
                "installed": False,
            },
        ])

        self.assertIn("Installed (1)", output)
        self.assertIn("Uninstalled (2)", output)
        self.assertLess(output.index("Installed (1)"), output.index("Uninstalled (2)"))
        self.assertLess(output.index("  STT (1)"), output.index("Uninstalled (2)"))
        self.assertLess(output.index("  STT (1)", output.index("Uninstalled (2)")), output.index("  TTS (1)"))
        self.assertIn("    - SenseVoice Small INT8 (sensevoice-small-local) | zh, en | sherpa-onnx", output)
        self.assertIn("    - Paraformer Chinese (paraformer-zh-local) | zh | funasr", output)
        self.assertIn("    - Melo TTS (melo-tts-zh-en-local) | zh, en | sherpa-onnx", output)
        self.assertIn("zh, en", output)
        self.assertNotRegex(output, r"[\u4e00-\u9fff]")

    def test_empty_model_list_has_helpful_message(self):
        self.assertEqual(_format_model_list([]), "The model catalog is empty.")


if __name__ == "__main__":
    unittest.main()
