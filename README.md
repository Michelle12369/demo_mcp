# demo_mcp — 品質資料 MCP gateway(FastMCP + OpenAPI + Skills)

一個示範用的 **MCP gateway**:以 `FastMCP.from_openapi` 讀 OpenAPI spec 自動生成 MCP tool,
實際查詢時透過 httpx 轉打到後端 mock server;並掛上 SkillsDirectoryProvider 提供使用 skill。
資料全為合成(mock),用於端對端測試 connector / agent 流程。

## 架構

```
agent ──MCP──▶ gateway.py (:8200/mcp)                        ← 唯一對外的 MCP 端點
                 │  FastMCP.from_openapi(openapi.json)        ← 依 spec 生成 5 隻 tool
                 │  + SkillsDirectoryProvider                 ← SKILL.md / references/weeks.md
                 ▼  httpx(base_url = MOCK_BASE_URL)
              mock_server.py (:8100, Starlette REST)          ← 資料層,依參數真過濾
                 ▼
              data/*.json                                     ← 現讀,改檔即改回覆
```

## 檔案

| 檔案 | 說明 |
|---|---|
| `gateway.py` | MCP gateway 入口。`from_openapi` 生成 tool + SkillsProvider。內含 `SKILL_MARKDOWN` / `WEEKS_REFERENCE`(skill 真相來源,啟動時 materialize 到 `skills/`) |
| `mock_server.py` | 後端 REST(Starlette)。路由對齊 `openapi.json`,回覆讀 `data/*.json` 並依參數真過濾 |
| `openapi.json` | 5 個 REST operation 的 spec(operationId = tool 名) |
| `data/fabs.json` | fab 主檔(3 個:FAB_A/B/C,含 region/tech_node/active) |
| `data/devices.json` | device 主檔(50 個 × 10 欄;僅 DEV-01~08 有量測資料) |
| `data/quality.json` | 量測資料(3 fab × 4 週 = 1920 列;yield×defect 負相關,含 2 個植入異常) |

> `skills/` 為 gateway 啟動時自動 materialize 的產物(已於 `.gitignore` 排除),
> 真相來源是 `gateway.py` 的常數。

## 安裝

```bash
python3 -m venv .venv
./.venv/bin/pip install fastmcp uvicorn httpx starlette
```

## 啟動(兩個 process,先後端再 gateway)

```bash
# Terminal 1 — 後端 mock server
./.venv/bin/python mock_server.py      # http://127.0.0.1:8100

# Terminal 2 — MCP gateway
./.venv/bin/python gateway.py          # http://127.0.0.1:8200/mcp
```

- 後端位址可用環境變數覆寫:`MOCK_BASE_URL=http://host:port ./.venv/bin/python gateway.py`
- gateway 啟動時**不會**檢查後端是否在線(tool 被呼叫時才連),請先起 `mock_server.py`。

## 5 隻 tool

feeder(純 lookup,依參數真過濾):

- `list_fabs(region?, active?, name_contains?)` — fab 候選
- `list_devices(fab?, lifecycle_status?, name_contains?)` — device 主檔候選(50 個,含雜訊欄)
- `list_stations(fab?, device?, name_contains?)` — station 候選
- `list_weeks(fab?, recent?)` — 實際有資料的週別

主查詢:

- `get_quality(fab, device, week)` — 依 fab/week/device 真過濾;**三者皆可傳 list**
  (各自 OR、維度間 AND),方便一次比較多廠/多週/多 device。三個必填條件:`fab`←list_fabs、
  `device`←list_devices、`week` 見 skill(2026-W29~W32)。某維度全部無效回可行動 `errorCode`。
  回傳量測列在 `data.queryResult`。

feeder 回傳皆為信封 `{"data": [...], "errorCode": ""}`;`get_quality` 為
`{"data": {"queryResult": [...]}, "errorCode": ""}`。

## 資料特性(供分析型 demo)

- 廠體質:FAB_A(N7)≈98% > FAB_B(N5)≈96% > FAB_C(N3 新廠)≈94%
- 週趨勢:FAB_C 明顯 ramp(W29→W32)
- yield 與 defect_count 強負相關(≈ −0.95)
- 植入異常:① FAB_C/W30/ETCH-02 良率崩到 ~85% ② FAB_B/W31/Device Delta 崩到 ~89%
