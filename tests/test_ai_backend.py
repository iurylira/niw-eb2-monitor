import unittest

from scripts.classify_queue import (
    normalize_classification_payload,
    resolve_backend,
    sync_taxonomy_with_results,
)


class TestAIBackend(unittest.TestCase):
    def test_resolve_backend_prefers_ollama_when_available(self):
        self.assertEqual(
            resolve_backend({"ollama": True, "claude": True}, "auto"), "ollama"
        )

    def test_resolve_backend_falls_back_to_claude(self):
        self.assertEqual(
            resolve_backend({"ollama": False, "claude": True}, "auto"), "claude"
        )

    def test_resolve_backend_requires_valid_choice(self):
        with self.assertRaises(ValueError):
            resolve_backend({"ollama": False, "claude": False}, "auto")

    def test_normalize_classification_payload_fills_required_fields(self):
        payload = {
            "case_id": "123456",
            "decision_date": "2026-01-21",
            "file": "JAN212026_01B5203.pdf",
            "occupation": "AI engineer",
            "endeavor_type": "employed_professional",
            "outcome": "dismissed",
            "eb2_classification_met": True,
            "dispositive_prong": 1,
            "prongs_reserved": [2, 3],
            "denial_reasons": ["P1_FIELD_VS_ENDEAVOR_CONFLATION"],
            "key_quotes": ["impact of the field is not the same as the endeavor"],
            "lessons": ["Tie the endeavor to project-specific impact."],
        }

        normalized = normalize_classification_payload(payload)

        self.assertEqual(normalized["case_id"], "123456")
        self.assertEqual(normalized["occupation"], "AI engineer")
        self.assertIsInstance(normalized["denial_reasons"], list)
        self.assertIsInstance(normalized["lessons"], list)

    def test_sync_taxonomy_with_results_adds_new_denial_codes(self):
        taxonomy = {
            "fields": {
                "denial_reasons": [
                    "P1_NATIONAL_IMPORTANCE_NOT_SHOWN",
                    "P2_PLAN_VAGUE_OR_NOT_ACTIONABLE",
                ],
            },
        }
        results = [{
            "denial_reasons": [
                "P1_NATIONAL_IMPORTANCE_NOT_SHOWN",
                "P2_PLAN_VAGUE_OR_NOT_ACTIONABLE",
                "P3_NEW_REASON_DETECTED",
            ],
        }]

        updated = sync_taxonomy_with_results(results, taxonomy)

        self.assertIn("P3_NEW_REASON_DETECTED", updated["fields"]["denial_reasons"])
        self.assertEqual(len(updated["fields"]["denial_reasons"]), 3)

    def test_sync_taxonomy_corrects_wrong_prong_prefix_instead_of_forking(self):
        taxonomy = {
            "fields": {
                "denial_reasons": [
                    "P1_SHORTAGE_ARGUMENT_REJECTED",
                    "P2_POSITIONING_INSUFFICIENT",
                ],
            },
        }
        # A small model mislabeling existing codes under the wrong prong --
        # not genuinely new denial patterns.
        results = [{"denial_reasons": ["P3_SHORTAGE_ARGUMENT_REJECTED", "P1_POSITIONING_INSUFFICIENT"]}]

        updated = sync_taxonomy_with_results(results, taxonomy)

        self.assertEqual(len(updated["fields"]["denial_reasons"]), 2)
        self.assertNotIn("P3_SHORTAGE_ARGUMENT_REJECTED", updated["fields"]["denial_reasons"])
        self.assertNotIn("P1_POSITIONING_INSUFFICIENT", updated["fields"]["denial_reasons"])
        # The result itself gets corrected to the existing canonical codes.
        self.assertEqual(
            results[0]["denial_reasons"],
            ["P1_SHORTAGE_ARGUMENT_REJECTED", "P2_POSITIONING_INSUFFICIENT"],
        )


if __name__ == "__main__":
    unittest.main()
