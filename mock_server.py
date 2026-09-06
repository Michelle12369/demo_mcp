"""後端 mock server(Starlette REST, :8100)——gateway 的資料來源。

- 路由對齊 openapi.json 的 path;gateway.py 以 FastMCP.from_openapi 依該 spec 生成 MCP tool,
  實際查詢時透過 httpx 打到本 server。
- 回覆皆來自本檔旁 data/ 下的 JSON mock 檔,**每次呼叫現讀**——直接編輯 data/fabs.json、
  data/quality.json 就能改回覆,不用重啟。
- 所有 endpoint(含 /quality)皆**依參數真過濾**:/quality 依 fab/device/week 過濾,
  參數超出可用範圍時回可行動 errorCode、data 空;查無交集則正常回 data:[]。

用法:
    cd ~/Desktop/demo-mcp && ./.venv/bin/python mock_server.py   # http://127.0.0.1:8100
"""

import json
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PORT = 8100
DATA_DIR = Path(__file__).parent / "data"


def _load_mock(filename: str):
    return json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))


def _quality_rows() -> list[dict]:
    """quality.json 的量測列——device/station/week 候選的真實來源。"""
    return _load_mock("quality.json")["data"]


def _match(text: str, needle: str | None) -> bool:
    """name_contains 類的大小寫不敏感子字串比對;needle 為空視為不過濾。"""
    return needle is None or needle.strip() == "" or needle.lower() in (text or "").lower()


def _qbool(req: Request, key: str) -> bool | None:
    """把 query string 的 true/false/1/0 解析成 bool;未帶則 None。"""
    v = req.query_params.get(key)
    if v is None:
        return None
    return v.strip().lower() in ("1", "true", "yes")


def _qint(req: Request, key: str) -> int | None:
    v = req.query_params.get(key)
    return int(v) if v not in (None, "") else None


# ── /fabs (list_fabs) ─────────────────────────────────────────────────────────
async def list_fabs(req: Request) -> JSONResponse:
    region = req.query_params.get("region")
    active = _qbool(req, "active")
    name_contains = req.query_params.get("name_contains")
    out = []
    for f in _load_mock("fabs.json"):
        f = {**f, "active": f.get("active", True)}
        if region is not None and f.get("region") != region:
            continue
        if active is not None and f.get("active") != active:
            continue
        if not _match(f.get("name", ""), name_contains):
            continue
        out.append(f)
    return JSONResponse({"data": out, "errorCode": ""})


# ── /devices (list_devices) ───────────────────────────────────────────────────
async def list_devices(req: Request) -> JSONResponse:
    """從 device 主檔(data/devices.json,共 50 個)回候選,每個含 10 欄(id/name/fabs 為
    相關欄,其餘 vendor/package_type/... 為模擬真實後端的雜訊欄)。支援真過濾:
    - fab:           只留 fabs 欄含該 fab 的 device
    - lifecycle_status: 只留該生命週期狀態(如 active)
    - name_contains: 對 name 做大小寫不敏感子字串比對
    注意:只有前 8 個 device(DEV-01~08)在 quality.json 有量測資料。
    """
    fab = req.query_params.get("fab")
    status = req.query_params.get("lifecycle_status")
    name_contains = req.query_params.get("name_contains")
    out = []
    for d in _load_mock("devices.json"):
        if fab is not None and fab not in d.get("fabs", []):
            continue
        if status is not None and d.get("lifecycle_status") != status:
            continue
        if not _match(d["name"], name_contains):
            continue
        out.append(d)
    return JSONResponse({"data": out, "errorCode": ""})


# ── /stations (list_stations) ─────────────────────────────────────────────────
async def list_stations(req: Request) -> JSONResponse:
    fab = req.query_params.get("fab")
    device = req.query_params.get("device")
    name_contains = req.query_params.get("name_contains")
    seen: dict[str, dict] = {}
    for r in _quality_rows():
        if fab is not None and r["fab"] != fab:
            continue
        if device is not None and device not in (r["device"]["id"], r["device"]["name"]):
            continue
        if not _match(r["station"], name_contains):
            continue
        entry = seen.setdefault(r["station"], {"id": r["station"], "fabs": set(), "lot_count": 0})
        entry["fabs"].add(r["fab"])
        entry["lot_count"] += 1
    out = [
        {**e, "fabs": sorted(e["fabs"])}
        for e in sorted(seen.values(), key=lambda x: x["id"])
    ]
    return JSONResponse({"data": out, "errorCode": ""})


# ── /weeks (list_weeks) ───────────────────────────────────────────────────────
async def list_weeks(req: Request) -> JSONResponse:
    fab = req.query_params.get("fab")
    recent = _qint(req, "recent")
    counts: dict[str, int] = {}
    for r in _quality_rows():
        if fab is not None and r["fab"] != fab:
            continue
        counts[r["week"]] = counts.get(r["week"], 0) + 1
    weeks = [{"week": w, "lot_count": counts[w]} for w in sorted(counts, reverse=True)]
    if recent is not None and recent > 0:
        weeks = weeks[:recent]
    return JSONResponse({"data": weeks, "errorCode": ""})


# ── /quality (get_quality) — 依 fab/device/week 真過濾 ────────────────────────
async def get_quality(req: Request) -> JSONResponse:
    """依 fab / week / device 真過濾;device 吃 **list**(OR 語意:回符合任一 device 的所有列)。
    - fab/week 超出可用範圍 → 回可行動 errorCode、data 空。
    - device list 裡「全部」都無量測資料 → 回可行動 errorCode(有量測資料的僅 DEV-01~08)。
    - device list 裡「部分」無資料 → 靜默忽略無資料者,只回有資料者的列。
    - 命中 → 回符合的量測列。
    """
    fab = req.query_params.get("fab")
    week = req.query_params.get("week")
    devices = req.query_params.getlist("device")
    # 容錯:若以逗號分隔傳成單一字串
    if len(devices) == 1 and "," in devices[0]:
        devices = [d.strip() for d in devices[0].split(",") if d.strip()]

    rows = _quality_rows()
    valid_fabs = sorted({r["fab"] for r in rows})
    valid_weeks = sorted({r["week"] for r in rows})
    valid_devices = sorted({r["device"]["id"] for r in rows})

    def err(msg: str) -> JSONResponse:
        return JSONResponse({"data": [], "errorCode": msg})

    if not fab:
        return err("缺少必填參數 fab(呼叫 list_fabs 取得候選)")
    if not devices:
        return err("缺少必填參數 device(可傳多個;呼叫 list_devices 取得候選)")
    if not week:
        return err("缺少必填參數 week(可用週別見 skill 或 list_weeks)")
    if fab not in valid_fabs:
        return err(f"未知的 fab '{fab}'——可用 fab:{', '.join(valid_fabs)}(呼叫 list_fabs)")
    if week not in valid_weeks:
        return err(f"週別 '{week}' 無資料——可用週別:{', '.join(valid_weeks)}")

    known = [d for d in devices if d in valid_devices]
    if not known:
        return err(
            f"device {devices} 皆無量測資料——有量測資料的 device:{', '.join(valid_devices)}"
            "(呼叫 list_devices;注意主檔 50 個 device 僅 DEV-01~08 有量測)"
        )

    known_set = set(known)
    filtered = [
        {**r, "device": dict(r["device"])}
        for r in rows
        if r["fab"] == fab and r["week"] == week and r["device"]["id"] in known_set
    ]
    return JSONResponse({"data": filtered, "errorCode": ""})


async def health(req: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(routes=[
    Route("/fabs", list_fabs, methods=["GET"]),
    Route("/devices", list_devices, methods=["GET"]),
    Route("/stations", list_stations, methods=["GET"]),
    Route("/weeks", list_weeks, methods=["GET"]),
    Route("/quality", get_quality, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
])


if __name__ == "__main__":
    print(f"demo mock server (backend): http://127.0.0.1:{PORT} (mock data: {DATA_DIR})")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
