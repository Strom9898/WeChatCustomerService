import json
import tempfile
import unittest
from pathlib import Path

from legal_service import ArbitrationService


class ArbitrationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = ArbitrationService(
            Path(__file__).resolve().parents[1] / "customer_service",
            Path(self.temporary.name) / "profiles",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_labor_query_retrieves_only_arbitration_material(self):
        results = self.service.retrieve_knowledge("公司拖欠工资，离职后还能劳动仲裁吗")
        self.assertTrue(results)
        self.assertTrue(all("涂料" not in item["content"] for item in results))
        self.assertEqual(results[0]["category"], "劳动仲裁")

    def test_structured_reply_and_profile(self):
        raw = json.dumps({
            "case_update": {"procedure_type": "劳动仲裁", "stage": "初步咨询"},
            "reply_segments": ["嗯，明白了。", "先确认一下您什么时候离职。"],
        }, ensure_ascii=False)
        parsed = self.service.parse_model_response(raw, 3)
        self.assertEqual(parsed["reply_segments"], ["嗯，明白了。", "先确认一下您什么时候离职。"])
        profile = self.service.update_profile("测试群", parsed["case_update"])
        self.assertEqual(profile["procedure_type"], "劳动仲裁")

    def test_resolved_missing_information_is_not_kept_forever(self):
        self.service.update_profile("测试群", {
            "missing_information": ["每月工资", "实际工作地"],
        })
        profile = self.service.update_profile("测试群", {
            "missing_information": ["实际工作地"],
        })
        self.assertEqual(profile["missing_information"], ["实际工作地"])

    def test_malformed_json_is_not_sent_to_customer(self):
        parsed = self.service.parse_model_response('{"case_update": invalid}', 3)
        self.assertEqual(parsed["reply_segments"], [])


if __name__ == "__main__":
    unittest.main()
