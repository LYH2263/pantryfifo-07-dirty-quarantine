# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest` / `temp_zone` / `quarantine`（脏批隔离：dirty 或余量非正的批只进隔离入口，清洗预览不改状态，清洗确认与过期下架对同一行只留一种 status）。
