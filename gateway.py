"""MCP gateway(FastMCP.from_openapi + SkillsDirectoryProvider, :8200/mcp)。

架構:
    agent ──MCP──▶ gateway.py (本檔) ──httpx(依 openapi.json)──▶ mock_server.py (:8100) ──▶ data/*.json

- 以 `FastMCP.from_openapi` 讀本檔旁的 openapi.json,把每個 REST operation 自動生成 MCP tool
  (tool 名 = operationId:list_fabs / list_devices / list_stations / list_weeks / get_quality)。
- 實際查詢透過 httpx client 打到後端 mock server(MOCK_BASE_URL,預設 http://127.0.0.1:8100)。
- skills 用 SkillsDirectoryProvider(supporting_files="resources")——SKILL.md 由本檔 SKILL_MARKDOWN
  常數定義(啟動時 materialize),另帶 references/weeks.md 支援檔。
- Mongo catalog 的 demo_quality mcpUrl 指向 http://localhost:8200/mcp(gateway),不需更動。

用法(需先起後端 mock server):
    # Terminal 1
    cd ~/Desktop/demo-mcp && ./.venv/bin/python mock_server.py    # http://127.0.0.1:8100
    # Terminal 2
    cd ~/Desktop/demo-mcp && ./.venv/bin/python gateway.py        # http://127.0.0.1:8200/mcp
"""

import json
import os
from pathlib import Path

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.providers.skills import SkillsDirectoryProvider

PORT = 8200
MOCK_BASE_URL = os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:8100")
BASE_DIR = Path(__file__).parent
OPENAPI_SPEC = BASE_DIR / "openapi.json"

# SKILL.md 本文——面向模型,涵蓋 5 隻 tool 的語意、呼叫順序/相依、參數來源與範例。
SKILL_MARKDOWN = """---
name: demo-quality-usage-test
description: demo_quality connector 的使用skill——查詢/落表前必讀,涵蓋 5 隻 tool 清單與語意、呼叫順序與相依、參數來源、範例。
---

# demo_quality skill

## tools 清單與語意

說明:每支 tool 回傳皆為信封 `{"data": [...], "errorCode": ""}`——候選/量測列都在 `data` 陣列。
呼叫後回應會**自動落成一張表**(表名見回饋文字,通常是 `demo_quality_<tool>`,
如 `demo_quality_list_fabs`、`demo_quality_get_quality`);不需、也不用帶 `land_as` 參數。

feeder(純 lookup,自動落成一張**小表**,供反問使用者或縮小查詢範圍):

- `list_fabs(region?, active?, name_contains?)`：列出 fab 候選(id/name/region/tech_node/active)。
  參數皆選填、可自由組合並**真的過濾**。用於取得 `get_quality` 的 `fab`。
- `list_devices(fab?, lifecycle_status?, name_contains?)`：列出 device 主檔候選(共 50 個,
  每個含 10 欄——`id`/`name`/`fabs` 為相關欄,`vendor`/`package_type`/`wafer_size_mm`/
  `introduced_date`/`lifecycle_status`/`owner_team`/`category` 為雜訊欄,**取 `id` 即可、雜訊欄忽略**)。
  參數選填可組合、真過濾。用於取得 `get_quality` 的 `device`。
  ⚠ 主檔 50 個 device 中**僅 `DEV-01~08` 有量測資料**,其餘只是目錄項、get_quality 查不到列。
- `list_stations(fab?, device?, name_contains?)`：列出 station 候選(id + fabs + lot_count),
  用於縮小 device/quality 的查詢範圍。
- `list_weeks(fab?, recent?)`：列出實際有資料的週別(week + lot_count),用於確認 `week`。

主查詢(回應的 `data` 自動落成一張**大表**供分析):

- `get_quality(fab, device, week)`：取得指定 fab/week 與**一組 device** 的品質量測資料,回傳信封
  `{"data": [...量測列...], "errorCode": ""}`。`device` 為 **list**(可傳多個,OR 語意:回符合任一
  device 的所有列,方便一次比較多個 device)。每列含淺巢狀欄 `device: {"id","name"}`
  與 `fab`/`week`/`station`/`yield_pct`/`defect_count`/`measured_at`。
  `data` 自動落表(表名見回饋);`errorCode` 出現在回饋文字的「回應其他欄位」,
  非空時視為業務錯誤,需轉述使用者、不當作資料使用。

## 呼叫順序與相依

1. `get_quality` 有**三個必填條件**:`fab`、`device`、`week`——三個都齊了才可呼叫。
2. `fab` 未知 → 先 `list_fabs` 取候選;`device` 未知 → 先 `list_devices`
   (可帶 `fab=` 縮小)取候選。兩隻 feeder 只需取回傳中的 `id` 值。
3. `week` 見下「參數來源」;不確定該 fab 有哪些週別可先 `list_weeks(fab=...)`。
4. `get_quality` 回饋文字「回應其他欄位」的 `errorCode` 非空時代表業務層錯誤,
   agent 需轉述、不當作資料用。

## 落表後的 SQL(展開巢狀 data)

`get_quality` 自動落表後(表名如 `demo_quality_get_quality`),該表是**一列信封**——
`data` 欄是 array of struct、`errorCode` 是字串。分析前必須先用 UNNEST 把 `data`
攤平成一列一個量測:

```sql
SELECT unnest.* FROM demo_quality_get_quality, UNNEST(data) AS t(unnest)
```

- 表名以實際回饋為準(下例用 `demo_quality_get_quality`);`unnest.*` 會展開成
  `lot_id`/`fab`/`week`/`station`/`yield_pct`/`defect_count`/`measured_at` 與淺巢狀 `device`。
- `device` 仍是 struct,取欄位用 `unnest.device.id`、`unnest.device.name`。
- 常見做法:把展開結果當 CTE/子查詢,再接聚合。例如各站平均良率:

```sql
WITH q AS (SELECT unnest.* FROM demo_quality_get_quality, UNNEST(data) AS t(unnest))
SELECT station, AVG(yield_pct) AS avg_yield, AVG(defect_count) AS avg_def
FROM q GROUP BY station ORDER BY avg_yield;
```

## 參數來源

- `fab`  ← `list_fabs` 回傳任一物件的 `id`;使用者也可能直接指名 fab 代號,可略過 list_fabs。
- `device`← `list_devices` 回傳物件的 `id`(**取 id 即可,其餘雜訊欄忽略**);**可傳多個**
  (list,OR 語意,一次比較多個 device);僅 `DEV-01~08` 有量測資料。
- `week` ← ISO 週別字串 `YYYY-Www`。可用範圍 **2026-W29 ~ 2026-W32**,未指定時預設最新週
  **2026-W32**(詳見 references/weeks.md);可用 `list_weeks` 確認實際有資料的週別。

## 範例

使用者:「幫我看 Fab A 的 Device Alpha 這週品質」

1. `list_fabs(name_contains="Fab A")` → 從自動落的小表確認 `id="FAB_A"`。
2. `list_devices(fab="FAB_A", name_contains="Alpha")` → 確認 `id="DEV-01"`。
3. week 未明講 → 依 skill 預設最新週 `2026-W32`(或先 `list_weeks(fab="FAB_A")` 確認)。
4. `get_quality(fab="FAB_A", device=["DEV-01"], week="2026-W32")` → 回饋給出表名
   (如 `demo_quality_get_quality`),`data` 已自動落表。
   (要一次比較多個 device 時,`device` 傳 list,如 `["DEV-01","DEV-02"]`。)
5. 先展開再分析:
   `SELECT unnest.* FROM demo_quality_get_quality, UNNEST(data) AS t(unnest)`
   (見「落表後的 SQL」),之後即可對展開結果下 SQL 分析良率與缺陷分布。
"""

# get_quality 的第三個查詢條件(week)刻意寫在 skill 支援檔,讓模型從 skill 取得語意/預設。
WEEKS_REFERENCE = """# 可用週別參考(get_quality 的第三個條件 week)

- 可用範圍:2026-W29 ~ 2026-W32(ISO 週別,格式 YYYY-Www)
- 沒特別指定時,預設用最新一週 2026-W32
- 範圍外的週別 get_quality 會回可行動錯誤並列出可用週別
- 想確認某 fab 實際有資料的週別,可先呼叫 list_weeks(fab=...)
"""


def materialize_skills_dir() -> Path:
    skills_root = BASE_DIR / "skills"
    usage_dir = skills_root / "usage-test"
    (usage_dir / "references").mkdir(parents=True, exist_ok=True)
    (usage_dir / "SKILL.md").write_text(SKILL_MARKDOWN, encoding="utf-8")
    (usage_dir / "references" / "weeks.md").write_text(WEEKS_REFERENCE, encoding="utf-8")
    return skills_root


def build_gateway() -> FastMCP:
    """讀 openapi.json,用 FastMCP.from_openapi 生成 gateway,並掛上 skill provider。"""
    spec = json.loads(OPENAPI_SPEC.read_text(encoding="utf-8"))
    client = httpx.AsyncClient(base_url=MOCK_BASE_URL, timeout=30.0)
    gateway = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="demo-quality",
        validate_output=False,  # 回覆信封 {data, errorCode} 不套 schema 驗證
    )
    gateway.add_provider(
        SkillsDirectoryProvider(roots=materialize_skills_dir(), supporting_files="resources")
    )
    return gateway


if __name__ == "__main__":
    gateway = build_gateway()
    print(f"demo MCP gateway: http://127.0.0.1:{PORT}/mcp  → backend {MOCK_BASE_URL}")
    print(f"(先確認後端已啟動:./.venv/bin/python mock_server.py)")
    uvicorn.run(gateway.http_app(stateless_http=True), host="127.0.0.1", port=PORT)
