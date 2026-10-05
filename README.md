# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架；脏批先隔离、清洗后再决定去向。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

- 0-1：`shopping_list` / `recipe_suggest` / `temp_zone`。
- 0-2：`quarantine` 脏批隔离。`data_quality='dirty'` 或余量非正的批只在
  隔离入口（`/quarantine`、`GET /api/quarantine`）出现，不进全层正区、
  不进 FEFO 按临期候选，顶条也不把它们当普通到期紧急。
  - `GET /api/quarantine/{id}/preview`：清洗预览，只读，不改 status。
  - `POST /api/quarantine/{id}/clean`（`restore`/`discard`）：
    核实余量为正且未过期 → 回正区；报废或批日历已过期 → 收口为 `expired`，
    从正区与顶条消失；校验失败 → 回滚，批留隔离，正区条数不变。
  - 清洗与过期下架对同一 dirty 行靠行锁 + SQL CAS 收口，最终只留一种 status。
