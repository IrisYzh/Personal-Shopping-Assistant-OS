import unittest

from shopping_agent.evaluation import (
    badcase_analysis,
    evaluate_baseline,
    evaluate_guardrail_v2,
    markdown_report,
    summarize,
)


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = evaluate_baseline()
        cls.summary = summarize(cls.results)
        cls.guardrail = evaluate_guardrail_v2(cls.results)
        cls.guardrail_summary = summarize(cls.guardrail)

    def test_product_jsonld_baseline_is_exactly_twenty_five_of_thirty_three(self):
        correct = sum(row["correct"] for row in self.results["product"])
        self.assertEqual(correct, 25)
        self.assertEqual(len(self.results["product"]), 33)
        self.assertEqual(self.summary["product_recognition_accuracy"], 0.7576)

    def test_deterministic_reminder_engine_has_no_false_alerts(self):
        self.assertEqual(self.summary["reminder_precision"], 1.0)
        self.assertEqual(self.summary["reminder_recall"], 1.0)

    def test_legacy_badcase_distribution_is_sixty_forty(self):
        analysis = badcase_analysis(self.results, self.guardrail)
        self.assertEqual(analysis["legacy_badcase_count"], 10)
        self.assertEqual(analysis["root_cause_distribution"]["intent_miss"]["share"], 0.6)
        self.assertEqual(analysis["root_cause_distribution"]["entity_extraction"]["share"], 0.4)
        self.assertEqual(analysis["closure_rate"], 1.0)

    def test_v2_product_pipeline_reaches_thirty_of_thirty_three(self):
        correct = sum(row["correct"] for row in self.guardrail["product"])
        self.assertEqual(correct, 30)
        self.assertEqual(self.guardrail_summary["product_recognition_accuracy"], 0.9091)

    def test_report_includes_agent_quality_and_residual_badcases(self):
        payload = {
            "generated_at": "2026-08-20T00:00:00+00:00",
            "agent_status": "available",
            "baseline_summary": self.summary,
            "guardrail_v2_summary": self.guardrail_summary,
            "agent_summary": self.guardrail_summary,
            "badcase_analysis": badcase_analysis(self.results, self.guardrail),
            "guardrail_v2_results": self.guardrail,
            "agent_results": {
                "intent": [
                    {
                        "id": "i07",
                        "correct": False,
                        "actual_route": "ask_confirmation",
                        "verification": {
                            "reasons": ["variant is missing"],
                            "missing_fields": ["product.size"],
                        },
                    }
                ],
                "product": [],
                "reminder": [],
                "verifier": [],
            },
        }
        report = markdown_report(payload)
        self.assertIn("意图分类与路由准确率", report)
        self.assertIn("Verifier 合法提案误拦截率", report)
        self.assertIn("三 Agent 当前残余 Badcase", report)
        self.assertIn("Product schema 没有 size 字段", report)


if __name__ == "__main__":
    unittest.main()
