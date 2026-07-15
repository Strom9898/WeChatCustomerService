import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from conversation_store import ConversationStore
from legal_service import ArbitrationService
from main import WeComAssistant


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, content):
        self.content = content

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class ReplyReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = ConversationStore(root / "conversations")
        self.service = ArbitrationService(
            Path(__file__).resolve().parents[1] / "customer_service",
            root / "profiles",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_same_unanswered_customer_turn_is_not_saved_twice(self):
        _, first_added = self.store.append_user_if_new("测试群", "5000")
        _, second_added = self.store.append_user_if_new("测试群", "5000")
        self.assertTrue(first_added)
        self.assertFalse(second_added)
        self.assertEqual(len(self.store.messages("测试群")), 1)

        self.store.append("测试群", "assistant", "明白了。")
        _, third_added = self.store.append_user_if_new("测试群", "5000")
        self.assertTrue(third_added)

    def test_repair_removes_existing_consecutive_duplicate_turns(self):
        self.store.append("测试群", "user", "我已经离职了")
        self.store.append("测试群", "user", "我已经离职了")
        self.store.append("测试群", "assistant", "明白了。")
        self.assertEqual(self.store.repair_saved_messages(), 1)
        self.assertEqual(
            [item["content"] for item in self.store.messages("测试群")],
            ["我已经离职了", "明白了。"],
        )

    def test_invalid_json_result_is_retried_automatically(self):
        assistant = WeComAssistant.__new__(WeComAssistant)
        assistant.ai_key = "test-key"
        assistant.ai_base_url = "https://example.invalid"
        assistant.ai_model = "test-model"
        assistant.system_prompt = ""
        assistant.memory_enabled = True
        assistant.memory_history_limit = 12
        assistant.humanized_reply_enabled = True
        assistant.max_reply_segments = 3
        assistant.ai_reply_attempts = 3
        assistant.conversations = self.store
        assistant.arbitration_service = self.service
        logs = []
        assistant._log = lambda level, message: logs.append((level, message))

        valid = json.dumps({
            "case_update": {"procedure_type": "劳动仲裁"},
            "reply_segments": ["好的，我接着帮您梳理。"],
        }, ensure_ascii=False)
        responses = [FakeResponse('{"case_update": {}}'), FakeResponse(valid)]

        with patch("main.requests.post", side_effect=responses) as mocked_post, patch("main.time.sleep"):
            result = assistant._generate_reply("5000", "测试群")

        self.assertEqual(result["reply_segments"], ["好的，我接着帮您梳理。"])
        self.assertEqual(mocked_post.call_count, 2)
        self.assertTrue(any("自动重试" in message for _, message in logs))

    def test_ocr_keeps_the_stronger_preprocessing_result(self):
        class FakeOcr:
            def __init__(self):
                self.calls = 0

            def readtext(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return [(None, "工作群的聊天记录，工牌照片我都还留着", 0.70)]
                return [(None, "工作群钓聊天记录", 0.08)]

        assistant = WeComAssistant.__new__(WeComAssistant)
        assistant.ocr = FakeOcr()
        assistant._log = lambda *_args: None
        result = assistant._ocr_image(Image.new("RGB", (254, 34), "#e4e7eb"))
        self.assertEqual(result, "工作群的聊天记录，工牌照片我都还留着")

    def test_common_chat_ocr_errors_are_normalized(self):
        result = WeComAssistant._normalize_ocr_text(
            "工作群的聊夭记录，工牌照片我都还留着，我上个月己经禽职了"
        )
        self.assertEqual(
            result,
            "工作群的聊天记录，工牌照片我都还留着，我上个月已经离职了",
        )

    def test_consecutive_gray_bubbles_after_blue_are_returned_as_batch(self):
        assistant = WeComAssistant.__new__(WeComAssistant)
        assistant._log = lambda *_args: None
        image = Image.new("RGB", (600, 320), "#f5f7fa")
        draw = ImageDraw.Draw(image)
        draw.rectangle((120, 30, 540, 75), fill="#c9e7ff")
        draw.rectangle((30, 130, 310, 165), fill="#e4e7eb")
        draw.rectangle((30, 205, 360, 240), fill="#e4e7eb")

        batch = assistant._extract_message_batch(image)

        self.assertEqual(len(batch), 2)
        self.assertTrue(all(not is_blue for _, is_blue, _ in batch))

    def test_customer_batch_store_appends_only_unseen_suffix(self):
        first = "公司承诺一月份结清，但是一直没有给"
        second = "这个承诺只有微信聊天记录，可以作为证据吗"
        self.store.append_user_batch_if_new("测试群", [first])
        added = self.store.append_user_batch_if_new("测试群", [first, second])
        self.assertEqual([item["content"] for item in added], [second])
        self.assertEqual(
            [item["content"] for item in self.store.messages("测试群")],
            [first, second],
        )


if __name__ == "__main__":
    unittest.main()
