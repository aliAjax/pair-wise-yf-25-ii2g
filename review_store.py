"""评审系统的 SQLite 存取与领域规则。

评分规则本身在 scoring.py，本模块只负责落库、状态机、权限与可见性。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from scoring import (
    DIMENSIONS,
    ScoreError,
    composite_score,
    dimension_averages,
    validate_scores,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "review.db"
VALID_DECISIONS = {"accept", "reject", "minor_revision", "major_revision"}


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# assignments 表的评分/草稿列；供建表与旧库迁移共用。
_DIMENSION_COLUMNS = ("score_innovation", "score_rigor", "score_reproducibility")
_DRAFT_COLUMNS = ("draft_scores", "draft_text", "draft_updated_at")
_SCHEMA_COLUMNS = _DIMENSION_COLUMNS + _DRAFT_COLUMNS + ("score_total",)


class ReviewStore:
    """领域逻辑。每个公开方法使用独立连接，避免 HTTP 线程共享 SQLite 连接。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')),
                    load_limit INTEGER NOT NULL DEFAULT 3 CHECK (load_limit >= 0)
                );
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','decided','withdrawn')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (paper_id, version)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS bids (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    interest TEXT NOT NULL CHECK (interest IN ('want','maybe','decline')),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'invited'
                        CHECK (status IN ('invited','accepted','declined','completed')),
                    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
                    score_innovation INTEGER
                        CHECK (score_innovation IS NULL OR score_innovation BETWEEN 1 AND 5),
                    score_rigor INTEGER
                        CHECK (score_rigor IS NULL OR score_rigor BETWEEN 1 AND 5),
                    score_reproducibility INTEGER
                        CHECK (score_reproducibility IS NULL OR score_reproducibility BETWEEN 1 AND 5),
                    score_total REAL,
                    draft_scores TEXT,
                    draft_text TEXT NOT NULL DEFAULT '',
                    draft_updated_at TEXT,
                    review_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (paper_id, reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS rebuttals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    author_id TEXT NOT NULL REFERENCES users(id),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
                    note TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (paper_id) REFERENCES papers(id)
                );
                """
            )
            # 兼容在本特性之前创建的旧库：逐列补齐三维度与草稿字段。
            existing = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(assignments)").fetchall()
            }
            for column in _SCHEMA_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE assignments ADD COLUMN {column}")

    def seed(self) -> None:
        self.init_schema()
        users = [
            ("alice", "Alice 作者", "author", 0),
            ("bob", "Bob 作者", "author", 0),
            ("r1", "评审人一号", "reviewer", 3),
            ("r2", "评审人二号", "reviewer", 3),
            ("r3", "评审人三号", "reviewer", 2),
            ("chair", "程序委员会主席", "chair", 0),
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)", users
            )

    def _user(self, conn: sqlite3.Connection, user_id: str | None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id 请求头", 401, "authentication_required")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise BusinessError("用户不存在", 401, "unknown_user")
        return row

    @staticmethod
    def _require(row: sqlite3.Row, role: str) -> None:
        if row["role"] != role:
            raise BusinessError(f"该操作仅允许 {role} 角色", 403, "forbidden")

    def _audit(self, conn: sqlite3.Connection, paper_id: int | None, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (paper_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def submit_paper(self, user_id: str, title: str, abstract: str) -> dict:
        title, abstract = title.strip(), abstract.strip()
        if len(title) < 3 or len(abstract) < 20:
            raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
        digest = hashlib.sha256(f"{title}\n{abstract}".encode()).hexdigest()
        with self.connect() as conn:
            user = self._user(conn, user_id)
            self._require(user, "author")
            cur = conn.execute(
                "INSERT INTO papers(author_id,title,abstract,created_at) VALUES(?,?,?,?)",
                (user_id, title, abstract, utcnow()),
            )
            paper_id = cur.lastrowid
            conn.execute(
                "INSERT INTO paper_versions(paper_id,version,content_hash,created_at) VALUES(?,?,?,?)",
                (paper_id, 1, digest, utcnow()),
            )
            self._audit(conn, paper_id, user_id, "paper.submit", {"version": 1, "sha256": digest})
            return {"id": paper_id, "status": "submitted", "version": 1, "sha256": digest}

    def _paper_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, viewer: sqlite3.Row) -> dict:
        data = {
            "id": paper["id"],
            "title": paper["title"],
            "abstract": paper["abstract"],
            "status": paper["status"],
            "created_at": paper["created_at"],
        }
        if viewer["role"] == "chair" or viewer["id"] == paper["author_id"]:
            data["author_id"] = paper["author_id"]
        else:
            data["author_id"] = None  # 双盲：评审人看不到作者身份。
        return data

    def list_papers(self, user_id: str) -> list[dict]:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "chair":
                rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
            elif user["role"] == "author":
                rows = conn.execute("SELECT * FROM papers WHERE author_id=? ORDER BY id", (user_id,)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT p.* FROM papers p
                       LEFT JOIN assignments a ON a.paper_id=p.id AND a.reviewer_id=?
                       LEFT JOIN bids b ON b.paper_id=p.id AND b.reviewer_id=?
                       WHERE a.id IS NOT NULL OR b.paper_id IS NOT NULL ORDER BY p.id""",
                    (user_id, user_id),
                ).fetchall()
            return [self._paper_view(conn, row, user) for row in rows]

    def get_paper(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "reviewer":
                allowed = conn.execute(
                    "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? UNION SELECT 1 FROM bids WHERE paper_id=? AND reviewer_id=?",
                    (paper_id, user_id, paper_id, user_id),
                ).fetchone()
                if not allowed:
                    raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")
            elif user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            return self._paper_view(conn, paper, user)

    def add_conflict(self, chair_id: str, paper_id: int, reviewer_id: str, reason: str) -> dict:
        if not reason.strip():
            raise BusinessError("利益冲突原因不能为空", 422, "invalid_reason")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise BusinessError("论文不存在", 404, "not_found")
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            try:
                conn.execute(
                    "INSERT INTO conflicts(reviewer_id,paper_id,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                    (reviewer_id, paper_id, reason.strip(), chair_id, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("利益冲突已登记", 409, "conflict_exists")
            self._audit(conn, paper_id, chair_id, "conflict.add", {"reviewer_id": reviewer_id, "reason": reason.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "reason": reason.strip()}

    def bid(self, reviewer_id: str, paper_id: int, interest: str, note: str = "") -> dict:
        if interest not in {"want", "maybe", "decline"}:
            raise BusinessError("意向必须为 want、maybe 或 decline", 422, "invalid_interest")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            paper = conn.execute("SELECT status FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["status"] not in {"submitted", "under_review"}:
                raise BusinessError("论文不存在或当前不可表达意向", 409, "paper_unavailable")
            if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                raise BusinessError("存在利益冲突，不能表达评审意向", 409, "conflict_of_interest")
            conn.execute(
                """INSERT INTO bids(reviewer_id,paper_id,interest,note,created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(reviewer_id,paper_id) DO UPDATE SET interest=excluded.interest,note=excluded.note,created_at=excluded.created_at""",
                (reviewer_id, paper_id, interest, note.strip(), utcnow()),
            )
            self._audit(conn, paper_id, reviewer_id, "bid.set", {"interest": interest, "note": note.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "interest": interest}

    def assign(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或不可分配", 409, "paper_unavailable")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                    raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
                load = conn.execute(
                    "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
                    (reviewer_id,),
                ).fetchone()[0]
                if load >= reviewer["load_limit"]:
                    raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
                try:
                    cur = conn.execute(
                        "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
                        (paper_id, reviewer_id, utcnow(), utcnow()),
                    )
                except sqlite3.IntegrityError:
                    raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                assignment_id = cur.lastrowid
                self._audit(conn, paper_id, chair_id, "assignment.invite", {"assignment_id": assignment_id, "reviewer_id": reviewer_id})
                return {"id": assignment_id, "paper_id": paper_id, "reviewer_id": reviewer_id, "status": "invited"}
            except Exception:
                conn.rollback()
                raise

    def respond_assignment(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "invited":
                raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
            status = "accepted" if accepted else "declined"
            conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?", (status, utcnow(), assignment_id))
            self._audit(conn, row["paper_id"], reviewer_id, "assignment.respond", {"assignment_id": assignment_id, "status": status})
            return {"id": assignment_id, "status": status}

    # ---- 评审草稿与提交 -------------------------------------------------

    @staticmethod
    def _dimension_scores(row: sqlite3.Row) -> dict[str, int] | None:
        values = {
            "innovation": row["score_innovation"],
            "rigor": row["score_rigor"],
            "reproducibility": row["score_reproducibility"],
        }
        if any(v is None for v in values.values()):
            return None
        return values

    @staticmethod
    def _draft_scores(row: sqlite3.Row) -> dict[str, int]:
        if not row["draft_scores"]:
            return {}
        try:
            data = json.loads(row["draft_scores"])
        except json.JSONDecodeError:
            return {}
        return {d: data[d] for d in DIMENSIONS if isinstance(data.get(d), int) and not isinstance(data[d], bool)}

    def _own_assignment(self, conn: sqlite3.Connection, reviewer_id: str, assignment_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        if not row or row["reviewer_id"] != reviewer_id:
            raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
        return row

    @staticmethod
    def _review_payload(row: sqlite3.Row) -> dict:
        """一份已提交评审的对外视图；旧评审只有原总分，没有维度分。"""
        scores = ReviewStore._dimension_scores(row)
        return {
            "assignment_id": row["id"],
            "paper_id": row["paper_id"],
            "reviewer_id": row["reviewer_id"],
            "legacy": scores is None,  # 三维度上线前提交的旧评审。
            "scores": scores,
            "total_score": composite_score(scores) if scores is not None else row["score"],
            "review_text": row["review_text"],
            "submitted_at": row["updated_at"],
        }

    def get_my_review(self, reviewer_id: str, assignment_id: int) -> dict:
        """评审人取回自己的评审：已提交返回锁定内容，未提交返回可续写草稿。"""
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = self._own_assignment(conn, reviewer_id, assignment_id)
            if row["status"] == "completed":
                return {"submitted": True, "locked": True, "review": self._review_payload(row)}
            if row["status"] != "accepted":
                raise BusinessError("只有接受邀请后才能填写评审", 409, "invalid_assignment_state")
            return {
                "submitted": False,
                "locked": False,
                "assignment_id": assignment_id,
                "draft_scores": self._draft_scores(row),
                "draft_text": row["draft_text"] or "",
                "draft_updated_at": row["draft_updated_at"],
            }

    def save_review_draft(self, reviewer_id: str, assignment_id: int, scores: object, text: str = "") -> dict:
        try:
            cleaned = validate_scores(scores, partial=True)
        except ScoreError as exc:
            raise BusinessError(exc.message, 422, exc.code)
        text = text if isinstance(text, str) else ""
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = self._own_assignment(conn, reviewer_id, assignment_id)
            if row["status"] == "completed":
                raise BusinessError("评审已提交，内容原样锁定，不能再修改", 409, "review_locked")
            if row["status"] != "accepted":
                raise BusinessError("只有接受邀请后才能保存草稿", 409, "invalid_assignment_state")
            # 草稿可续写：新给的维度覆盖旧值，未给的维度保留。
            merged = self._draft_scores(row)
            merged.update(cleaned)
            now = utcnow()
            conn.execute(
                "UPDATE assignments SET draft_scores=?,draft_text=?,draft_updated_at=?,updated_at=? WHERE id=?",
                (json.dumps(merged, sort_keys=True), text, now, now, assignment_id),
            )
            return {
                "id": assignment_id,
                "submitted": False,
                "saved": True,
                "draft_scores": merged,
                "draft_updated_at": now,
            }

    def submit_review(self, reviewer_id: str, assignment_id: int, scores: object, text: str) -> dict:
        """提交三维度评审。三个维度各填 1-5 分，缺一不可；提交后原样锁定。"""
        try:
            cleaned = validate_scores(scores, partial=False)
        except ScoreError as exc:
            raise BusinessError(exc.message, 422, exc.code)
        if not isinstance(text, str) or len(text.strip()) < 10:
            raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
        total = composite_score(cleaned)
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = self._own_assignment(conn, reviewer_id, assignment_id)
            if row["status"] == "completed":
                raise BusinessError("评审已提交，内容原样锁定，不能再修改", 409, "review_locked")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受邀请的评审人可以提交评审", 409, "invalid_assignment_state")
            conn.execute(
                """UPDATE assignments
                   SET status='completed',
                       score_innovation=:innovation, score_rigor=:rigor,
                       score_reproducibility=:reproducibility, score_total=:total,
                       review_text=:text,
                       draft_scores=NULL, draft_text='', draft_updated_at=NULL,
                       updated_at=:now
                   WHERE id=:id""",
                {
                    "innovation": cleaned["innovation"],
                    "rigor": cleaned["rigor"],
                    "reproducibility": cleaned["reproducibility"],
                    "total": total,
                    "text": text.strip(),
                    "now": utcnow(),
                    "id": assignment_id,
                },
            )
            self._audit(
                conn, row["paper_id"], reviewer_id, "review.submit",
                {"assignment_id": assignment_id, "scores": cleaned, "total_score": total},
            )
            return {
                "id": assignment_id,
                "status": "completed",
                "scores": cleaned,
                "total_score": total,
            }

    def list_paper_reviews(self, user_id: str, paper_id: int) -> dict:
        """按角色返回评审可见性。

        - 主席：随时看到每份评审的维度分与加权总分（旧评审只有原总分）。
        - 作者：决定后只看到三项维度平均分，且只统计新制评审。
        - 评审人：只看到自己那份的草稿或已提交内容。
        """
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")

            if user["role"] == "chair":
                rows = conn.execute(
                    "SELECT * FROM assignments WHERE paper_id=? AND status='completed' ORDER BY id",
                    (paper_id,),
                ).fetchall()
                return {
                    "paper_id": paper_id,
                    "reviews": [self._review_payload(r) for r in rows],
                }

            if user["role"] == "author":
                if paper["author_id"] != user_id:
                    raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
                if paper["status"] != "decided":
                    raise BusinessError("决定公布后才能查看评审平均分", 403, "reviews_not_released")
                rows = conn.execute(
                    """SELECT score_innovation AS innovation,
                              score_rigor AS rigor,
                              score_reproducibility AS reproducibility
                       FROM assignments
                       WHERE paper_id=? AND status='completed'
                         AND score_innovation IS NOT NULL
                         AND score_rigor IS NOT NULL
                         AND score_reproducibility IS NOT NULL""",
                    (paper_id,),
                ).fetchall()
                new_rows = [dict(r) for r in rows]
                return {
                    "paper_id": paper_id,
                    "review_count": len(new_rows),
                    "dimension_averages": dimension_averages(new_rows),
                }

            self._require(user, "reviewer")
            allowed = conn.execute(
                "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? UNION SELECT 1 FROM bids WHERE paper_id=? AND reviewer_id=?",
                (paper_id, user_id, paper_id, user_id),
            ).fetchone()
            if not allowed:
                raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")
            row = conn.execute(
                "SELECT * FROM assignments WHERE paper_id=? AND reviewer_id=?",
                (paper_id, user_id),
            ).fetchone()
            if not row:
                return {"paper_id": paper_id, "review": None}
            if row["status"] == "completed":
                return {"paper_id": paper_id, "review": self._review_payload(row)}
            return {
                "paper_id": paper_id,
                "review": {
                    "assignment_id": row["id"],
                    "submitted": False,
                    "draft_scores": self._draft_scores(row),
                    "draft_text": row["draft_text"] or "",
                    "draft_updated_at": row["draft_updated_at"],
                },
            }

    def submit_rebuttal(self, author_id: str, paper_id: int, content: str) -> dict:
        if len(content.strip()) < 10:
            raise BusinessError("Rebuttal 至少 10 字", 422, "rebuttal_too_short")
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["author_id"] != author_id:
                raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
            completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
            if completed < 1:
                raise BusinessError("至少收到一份完整评审后才能提交 Rebuttal", 409, "reviews_not_ready")
            try:
                cur = conn.execute(
                    "INSERT INTO rebuttals(paper_id,author_id,content,created_at) VALUES(?,?,?,?)",
                    (paper_id, author_id, content.strip(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("每篇论文只能提交一次 Rebuttal", 409, "rebuttal_exists")
            self._audit(conn, paper_id, author_id, "rebuttal.submit", {"rebuttal_id": cur.lastrowid})
            return {"id": cur.lastrowid, "paper_id": paper_id, "content": content.strip()}

    def decide(self, chair_id: str, paper_id: int, decision: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise BusinessError("决定值不合法", 422, "invalid_decision")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或已经决定", 409, "paper_decided")
                # 旧评审（只有总分）与新制三维度评审都计为一份有效评审。
                completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
                if completed < 2:
                    raise BusinessError("至少需要两份已完成评审才能作出决定", 409, "insufficient_reviews")
                cur = conn.execute(
                    "INSERT INTO decisions(paper_id,decision,note,decided_by,created_at) VALUES(?,?,?,?,?)",
                    (paper_id, decision, note.strip(), chair_id, utcnow()),
                )
                conn.execute("UPDATE papers SET status='decided' WHERE id=?", (paper_id,))
                self._audit(conn, paper_id, chair_id, "decision.record", {"decision": decision, "note": note.strip()})
                return {"id": cur.lastrowid, "paper_id": paper_id, "decision": decision, "note": note.strip()}
            except Exception:
                conn.rollback()
                raise

    def history(self, user_id: str, paper_id: int) -> list[dict]:
        self.get_paper(user_id, paper_id)  # 权限检查。
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log WHERE paper_id=? ORDER BY id", (paper_id,)).fetchall()
            return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]
