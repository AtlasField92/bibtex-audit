#!/usr/bin/env python3
"""Unit and functional tests for BibAudit Engine."""

import unittest
from pathlib import Path
from bib_audit_engine import (
    AuditSettings,
    Candidate,
    DecisionStore,
    ExecutionMode,
    SensitivitySettings,
    acceptance_reason,
    apply_candidate,
    clean_doi,
    normalize,
    parse_bibtex_file,
    process_entry,
    rank_candidates,
    score_candidate,
    similarity,
    strip_tex,
    valid_doi,
)


class TestBibAuditEngine(unittest.TestCase):

    def test_normalization_and_doi(self):
        self.assertEqual(clean_doi("https://doi.org/10.1016/j.patcog.2020.107567"), "10.1016/j.patcog.2020.107567")
        self.assertEqual(clean_doi("doi: 10.1145/3377325.3377503"), "10.1145/3377325.3377503")
        self.assertTrue(valid_doi("10.1016/j.patcog.2020.107567"))
        self.assertFalse(valid_doi("invalid_doi_1234"))

        tex = r"Deep \textbf{learning} for \textit{medical} image \& segmentation"
        self.assertEqual(strip_tex(tex), "Deep learning for medical image   segmentation")
        self.assertEqual(normalize(tex), "deep learning for medical image segmentation")

    def test_sensitivity_settings_and_presets(self):
        sens = SensitivitySettings()
        self.assertEqual(sens.title_strong, 0.94)

        # Strict preset
        sens.apply_preset("strict")
        self.assertEqual(sens.title_strong, 0.96)
        self.assertEqual(sens.margin_min, 0.020)

        # Permissive preset
        sens.apply_preset("permissive")
        self.assertEqual(sens.title_strong, 0.88)
        self.assertEqual(sens.margin_min, 0.006)

        # Reset to defaults
        sens.reset_defaults()
        self.assertEqual(sens.title_strong, 0.94)

    def test_scoring_and_candidate_ranking(self):
        entry = {
            "ID": "lecun2015deep",
            "ENTRYTYPE": "article",
            "title": "Deep Learning",
            "author": "Yann LeCun and Yoshua Bengio and Geoffrey Hinton",
            "year": "2015",
            "journal": "Nature",
        }

        c1 = Candidate(
            source="Crossref",
            title="Deep learning",
            doi="10.1038/nature14539",
            authors=[{"family": "LeCun", "given": "Yann"}, {"family": "Bengio", "given": "Yoshua"}, {"family": "Hinton", "given": "Geoffrey"}],
            year=2015,
            container="Nature",
        )

        c2 = Candidate(
            source="OpenAlex",
            title="Deep Learning in Neural Networks",
            doi="10.1016/j.neunet.2014.09.003",
            authors=[{"family": "Schmidhuber", "given": "Jürgen"}],
            year=2015,
            container="Neural Networks",
        )

        sens = SensitivitySettings()
        scored_c1 = score_candidate(entry, c1, sens)
        scored_c2 = score_candidate(entry, c2, sens)

        self.assertGreater(scored_c1.score, scored_c2.score)
        self.assertGreater(scored_c1.title_score, 0.95)

        ranked = rank_candidates(entry, [c2, c1], sens)
        self.assertEqual(ranked[0].doi, "10.1038/nature14539")

        reason = acceptance_reason(entry, ranked[0], margin=0.5, doi_verified=False, sensitivity=sens)
        self.assertTrue(reason.startswith(("reliable", "fiable")))

    def test_decision_store(self):
        import tempfile
        tmp = Path(tempfile.mktemp(suffix=".json"))
        try:
            store = DecisionStore(tmp)
            entry = {"ID": "test_key", "title": "A Great Paper"}
            store.record("test_key", choice="a", status="corrected", changes=["doi added"], note="Great", entry=entry)

            # Reload
            store2 = DecisionStore(tmp)
            self.assertEqual(store2.count(), 1)
            dec = store2.get(entry)
            self.assertIsNotNone(dec)
            self.assertEqual(dec["choice"], "a")
            self.assertEqual(dec["status"], "corrected")
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_parse_existing_bib(self):
        bib_file = Path("bib-complete-to-review.bib.txt")
        if bib_file.exists():
            db, err = parse_bibtex_file(bib_file)
            self.assertIsNone(err)
            self.assertIsNotNone(db)
            self.assertGreater(len(db.entries), 0)


if __name__ == "__main__":
    unittest.main()
