import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore
from scoring import ScoreError, composite_score, dimension_averages, validate_scores


GOOD = {"innovation": 4, "rigor": 3, "reproducibility": 5}
GOOD2 = {"innovation": 2, "rigor": 4, "reproducibility": 3}


class ScoringTests(unittest.TestCase):
    def test_weights_and_composite(self):
        self.assertEqual(composite_score(GOOD), round(4 * 0.4 + 3 * 0.35 + 5 * 0.25, 2))
        self.assertEqual(composite_score({k: 5 for k in GOOD}), 5.0)
        self.assertEqual(composite_score({k: 1 for k in GOOD}), 1.0)

    def test_submit_requires_all_three_dimensions(self):
        with self.assertRaises(ScoreError):
            validate_scores({"innovation": 4, "rigor": 3})
        with self.assertRaises(ScoreError):
            validate_scores({"innovation": 0, "rigor": 3, "reproducibility": 2})
        with self.assertRaises(ScoreError):
            validate_scores({"innovation": "4", "rigor": 3, "reproducibility": 2})

    def test_partial_allows_draft_gaps(self):
        self.assertEqual(validate_scores({"innovation": 4}, partial=True), {"innovation": 4})
        self.assertEqual(validate_scores({}, partial=True), {})
        with self.assertRaises(ScoreError):
            validate_scores({"rigor": 6}, partial=True)

    def test_dimension_averages_skip_missing(self):
        avgs = dimension_averages(
            [
                {"innovation": 5, "rigor": 3, "reproducibility": None},
                {"innovation": 3, "rigor": None, "reproducibility": None},
            ]
        )
        self.assertEqual(avgs, {"innovation": 4.0, "rigor": 3.0, "reproducibility": None})


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = ReviewStore(self.db_path)
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def _two_accepted(self, paper_id):
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        return a1, a2

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1, a2 = self._two_accepted(paper_id)
        r1 = self.store.submit_review("r1", a1, GOOD, "方法严谨，缺少与最近工作的对比。")
        self.assertEqual(r1["total"], composite_score(GOOD))
        self.store.submit_review("r2", a2, GOOD2, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 9)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError):
            self.store.assign("alice", paper_id, "r2")
        with self.assertRaises(BusinessError):
            self.store.get_paper("r2", paper_id)

    def test_missing_dimension_cannot_submit(self):
        paper_id = self._paper()
        a1, _ = self._two_accepted(paper_id)
        with self.assertRaises(ScoreError):
            self.store.submit_review("r1", a1, {"innovation": 4, "rigor": 3}, "意见长度足够的评审内容。")

    def test_draft_is_resumable_hidden_and_then_locked(self):
        paper_id = self._paper()
        a1, _ = self._two_accepted(paper_id)
        draft = self.store.save_review_draft("r1", a1, {"innovation": 4}, "先写一点。")
        self.assertEqual(draft["status"], "draft")
        self.assertFalse(draft["ready_to_submit"])

        # 续写：补剩余两个维度，不覆盖已有内容。
        self.store.save_review_draft("r1", a1, {"rigor": 3, "reproducibility": 5}, "先写一点。补充完整。")
        mine = self.store.get_my_review("r1", a1)
        self.assertEqual(mine["scores"], GOOD)
        self.assertFalse(mine["locked"])

        # 主席在提交前看不到草稿。
        self.assertEqual(self.store.list_reviews("chair", paper_id)["items"], [])
        # 其他评审人看不到别人的草稿。
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_my_review("r2", a1)
        self.assertEqual(ctx.exception.status, 404)

        submitted = self.store.submit_review("r1", a1, mine["scores"], "先写一点。补充完整。")
        self.assertEqual(submitted["status"], "completed")

        # 提交后原样锁定：不能再改，也不能再存草稿。
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_review("r1", a1, {k: 1 for k in GOOD}, "提交后试图覆盖的内容足够长。")
        self.assertEqual(ctx.exception.code, "review_locked")
        with self.assertRaises(BusinessError):
            self.store.save_review_draft("r1", a1, GOOD, "提交后试图覆盖的内容足够长。")
        locked = self.store.get_my_review("r1", a1)
        self.assertTrue(locked["locked"])
        self.assertEqual(locked["scores"], GOOD)

    def test_chair_sees_dimension_scores_and_total(self):
        paper_id = self._paper()
        a1, _ = self._two_accepted(paper_id)
        self.store.submit_review("r1", a1, GOOD, "方法严谨，缺少与最近工作的对比。")
        items = self.store.list_reviews("chair", paper_id)["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["scores"], GOOD)
        self.assertEqual(item["total"], composite_score(GOOD))
        self.assertFalse(item["legacy"])
        self.assertEqual(item["reviewer_id"], "r1")

    def test_author_sees_only_dimension_averages_after_decision(self):
        paper_id = self._paper()
        a1, a2 = self._two_accepted(paper_id)
        self.store.submit_review("r1", a1, GOOD, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, GOOD2, "实验充分，但部分结论需要进一步解释。")

        # 决定前作者看不到任何结果。
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_reviews("alice", paper_id)
        self.assertEqual(ctx.exception.code, "reviews_not_visible")

        self.store.decide("chair", paper_id, "accept", "表现优秀。")
        summary = self.store.list_reviews("alice", paper_id)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["averages"]["innovation"], 3.0)
        self.assertEqual(summary["averages"]["rigor"], 3.5)
        self.assertEqual(summary["averages"]["reproducibility"], 4.0)
        # 作者视图不含任何单份评审文本或评审人身份。
        self.assertNotIn("items", summary)

        # 其他论文的作者无权查看。
        with self.assertRaises(BusinessError):
            self.store.list_reviews("bob", paper_id)

    def test_reviewer_cannot_read_others_review_detail(self):
        paper_id = self._paper()
        a1, _ = self._two_accepted(paper_id)
        self.store.submit_review("r1", a1, GOOD, "方法严谨，缺少与最近工作的对比。")
        with self.assertRaises(BusinessError):
            self.store.list_reviews("r2", paper_id)

    def test_legacy_single_score_reviews_still_count_for_decision(self):
        paper_id = self._paper()
        a1, a2 = self._two_accepted(paper_id)
        # 模拟旧系统：只有 score 总分、维度分为 NULL 的已完成评审。
        with self.store.connect() as conn:
            for aid in (a1, a2):
                conn.execute(
                    "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                    (4, "旧评审系统留下的单总分评审意见。", "2026-01-01T00:00:00+00:00", aid),
                )
        # 旧评审仍满足"至少两份已完成评审"的决定门槛。
        result = self.store.decide("chair", paper_id, "accept", "按旧总分决定。")
        self.assertEqual(result["decision"], "accept")

        items = self.store.list_reviews("chair", paper_id)["items"]
        self.assertTrue(all(item["legacy"] for item in items))
        self.assertEqual(items[0]["total"], 4)
        self.assertIsNone(items[0]["scores"]["innovation"])

        # 评审人本人查看旧评审时同样回退到旧总分，而不是报合成错误。
        mine = self.store.get_my_review("r1", a1)
        self.assertTrue(mine["legacy"])
        self.assertEqual(mine["total"], 4)
        self.assertTrue(mine["locked"])

        summary = self.store.list_reviews("alice", paper_id)
        self.assertEqual(summary["count"], 2)
        self.assertTrue(all(v is None for v in summary["averages"].values()))

    def test_mixed_legacy_and_dimension_reviews(self):
        paper_id = self._paper()
        a1, a2 = self._two_accepted(paper_id)
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                (2, "旧评审系统留下的单总分评审意见。", "2026-01-01T00:00:00+00:00", a1),
            )
        self.store.submit_review("r2", a2, GOOD, "新评审的三维度意见，长度足够。")
        self.store.decide("chair", paper_id, "minor_revision", "混合评审。")
        summary = self.store.list_reviews("alice", paper_id)
        # 只有新评审贡献维度平均；旧评审被跳过而不是污染平均。
        self.assertEqual(summary["averages"]["innovation"], 4.0)
        self.assertEqual(summary["averages"]["rigor"], 3.0)
        self.assertEqual(summary["averages"]["reproducibility"], 5.0)

    def test_schema_migration_adds_dimension_columns(self):
        # 旧库（assignments 表没有维度列）经 init_schema 迁移后可正常使用。
        old_db = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(old_db) as conn:
            conn.execute(
                """CREATE TABLE users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                    load_limit INTEGER NOT NULL DEFAULT 3
                )"""
            )
            conn.execute(
                """CREATE TABLE papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, author_id TEXT NOT NULL,
                    title TEXT NOT NULL, abstract TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted', created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER NOT NULL,
                    reviewer_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'invited',
                    score INTEGER, review_text TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE (paper_id, reviewer_id)
                )"""
            )
        legacy = ReviewStore(old_db)
        legacy.init_schema()
        with sqlite3.connect(old_db) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(assignments)")}
        self.assertEqual(cols & {"innovation", "rigor", "reproducibility"},
                         {"innovation", "rigor", "reproducibility"})


if __name__ == "__main__":
    unittest.main()
