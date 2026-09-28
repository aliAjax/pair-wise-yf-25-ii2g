# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

访问 <http://127.0.0.1:8101>。默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review-draft`：暂存评审草稿，可多次续写，仅本人可见。
- `GET /api/assignments/{id}/review`：评审人取回本人草稿/已提交评审（提交后只读）。
- `POST /api/assignments/{id}/review`：提交三维度评审（创新性、严谨性、复现性，各 1-5 分），提交后原样锁定。
- `GET /api/papers/{id}/reviews`：主席看每份评审的维度分与合成总分；作者在决定后只看三项平均分。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

## 评分模型

评分计算集中在独立的 `scoring.py`（无数据库/HTTP 依赖）：三维度为创新性、严谨性、复现性，取值 1-5 整数，按 40% / 35% / 25% 合成总分。三项缺一不可提交；草稿允许只填部分。数据存取在 `app.py` 的 `ReviewStore`，页面在 `web/index.html`，三者分开维护。

可见性：草稿只有评审人本人可见；提交后内容锁定不可再改。主席可看到每份已提交评审的维度分与总分；作者在论文决定后只能看到各维度的跨评审平均分。旧系统只有单一总分的评审（维度分为空）不参与维度平均，但其总分仍计入决定门槛，主席视图会标记为 `legacy`。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。
