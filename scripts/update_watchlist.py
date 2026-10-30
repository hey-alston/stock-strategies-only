#!/usr/bin/env python3
"""每日自動更新選股池(Watchlist)。

流程:
1. 抓證交所 / 櫃買中心最近一個交易日的收盤價, 成交值, 以及公司基本資料(產業別, 股數)。
2. 只留下電子科技相關產業, 且 20 <= 收盤價 <= 1000, 成交值達門檻的普通股。
3. 以「成交值排名 + 市值排名」的綜合名次取前 N 檔(預設 40)。
   昨天已在池內的股票, 只要名次仍在 N+10 以內就會留下, 避免名單天天大幅跳動。
4. 與 ManualPicks 分頁(你自己挑的股票)合併, 寫進 Watchlist 分頁。
   同一檔股票兩邊都有時, 以 ManualPicks 為準。

安全設計:
- 任何一個資料來源失敗, 或算出來的池子太小, 就直接中止, 不會動 Watchlist。
- 第一次寫入前, 會把原本的 Watchlist 備份到 Watchlist_backup 分頁(只備份一次)。
- DRY_RUN=true 時只列印結果, 不寫入任何東西。

只依賴 gspread, 其餘皆為 Python 內建模組。
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

POOL_SIZE = int(os.environ.get("POOL_SIZE", "40"))
BUFFER = 10                      # 昨天的成員名次在 N+BUFFER 內就留下
MIN_PRICE = 20.0
MAX_PRICE = 1000.0
MIN_TRADE_VALUE = 1e8            # 最近一個交易日成交值至少 1 億元
MIN_POOL = 10                    # 低於這個數量就視為異常, 不寫入
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"

TWSE_PRICE = ["https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"]
TPEX_PRICE = ["https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"]
TWSE_INFO = ["https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
             "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv"]
TPEX_INFO = ["https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
             "https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv"]

TECH_CODES = {"24": "半導體", "25": "電腦及週邊", "26": "光電", "27": "通信網路",
              "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子"}
TECH_NAMES = list(TECH_CODES.values())

TW = timezone(timedelta(hours=8))


# ---------- 抓資料 ----------
def parse_table(text):
    text = text.lstrip("\ufeff").strip()
    if text[:1] in "[{":
        data = json.loads(text)
        return data if isinstance(data, list) else []
    return list(csv.DictReader(io.StringIO(text)))


def fetch_table(urls, tries=3):
    last = "未知錯誤"
    for url in urls:
        for _ in range(tries):
            r = subprocess.run(
                ["curl", "-sL", "--max-time", "60", "-A", "Mozilla/5.0", url],
                capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                try:
                    rows = parse_table(r.stdout)
                    if rows:
                        return rows
                    last = "%s 回傳空資料" % url
                except ValueError:
                    last = "%s 回傳內容無法解析" % url
            else:
                last = "%s 連線失敗" % url
            time.sleep(3)
    raise RuntimeError(last)


def find_key(rec, needles):
    for n in needles:
        if n in rec:
            return n
    for n in needles:
        for k in rec:
            if n.lower() in str(k).lower():
                return k
    return None


def need_keys(rows, spec, label):
    keys = {name: find_key(rows[0], needles) for name, needles in spec.items()}
    missing = [n for n, k in keys.items() if k is None]
    if missing:
        raise RuntimeError("%s 找不到欄位 %s, 實際欄位: %s" % (
            label, missing, list(rows[0].keys())))
    return keys


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


# ---------- 組出每檔股票的資料 ----------
def industry_name(v):
    v = str(v).strip()
    if v in TECH_CODES:
        return TECH_CODES[v]
    if v.isdigit() and v.zfill(2) in TECH_CODES:
        return TECH_CODES[v.zfill(2)]
    for n in TECH_NAMES:
        if n in v:
            return n
    return None


def load_market():
    stocks = {}   # code -> dict
    price_specs = [
        ("上市價格", TWSE_PRICE, {"code": ("Code",), "name": ("Name",),
                              "close": ("ClosingPrice",), "value": ("TradeValue",)}),
        ("上櫃價格", TPEX_PRICE, {"code": ("SecuritiesCompanyCode", "Code"),
                              "name": ("CompanyName", "Name"),
                              "close": ("Close", "ClosingPrice"),
                              "value": ("TransactionAmount", "TradeValue", "Amount")}),
    ]
    for label, urls, spec in price_specs:
        rows = fetch_table(urls)
        k = need_keys(rows, spec, label)
        for r in rows:
            code = str(r[k["code"]]).strip()
            close, value = num(r[k["close"]]), num(r[k["value"]])
            if code and close is not None and value is not None:
                stocks[code] = {"code": code, "name": str(r[k["name"]]).strip(),
                                "close": close, "value": value,
                                "cat": None, "cap": None}
        print("%s: 取得 %d 筆" % (label, len(rows)))

    info_specs = [
        ("上市基本資料", TWSE_INFO),
        ("上櫃基本資料", TPEX_INFO),
    ]
    spec = {"code": ("公司代號", "SecuritiesCompanyCode", "Code"),
            "ind": ("產業別", "SecuritiesIndustryCode", "IndustryCode", "Industry")}
    for label, urls in info_specs:
        rows = fetch_table(urls)
        k = need_keys(rows, spec, label)
        k_sh = find_key(rows[0], ("已發行普通股", "IssueShares", "IssuedShares", "Shares"))
        k_cap = find_key(rows[0], ("實收資本額", "Paidin", "PaidIn", "Capital"))
        for r in rows:
            code = str(r[k["code"]]).strip()
            if code not in stocks:
                continue
            stocks[code]["cat"] = industry_name(r[k["ind"]])
            shares = num(r[k_sh]) if k_sh else None
            if not shares and k_cap:
                cap_amt = num(r[k_cap])
                shares = cap_amt / 10.0 if cap_amt else None   # 面額 10 元
            if shares:
                stocks[code]["cap"] = shares * stocks[code]["close"]
        print("%s: 取得 %d 筆" % (label, len(rows)))
    return stocks


# ---------- 挑選 ----------
def eligible(stocks):
    out = []
    for s in stocks.values():
        if not re.fullmatch(r"\d{4}", s["code"]) or s["code"].startswith("91"):
            continue
        if not s["cat"]:
            continue
        if not (MIN_PRICE <= s["close"] <= MAX_PRICE):
            continue
        if s["value"] < MIN_TRADE_VALUE:
            continue
        out.append(s)
    return out


def rank_all(cands):
    capped = [c for c in cands if c["cap"]]
    use_cap = len(capped) >= 0.5 * len(cands) and len(capped) > 0
    base = capped if use_cap else cands
    by_val = sorted(base, key=lambda x: -x["value"])
    vr = {c["code"]: i for i, c in enumerate(by_val)}
    if use_cap:
        by_cap = sorted(base, key=lambda x: -x["cap"])
        cr = {c["code"]: i for i, c in enumerate(by_cap)}
        key = lambda c: (vr[c["code"]] + cr[c["code"]], vr[c["code"]])
    else:
        print("警告: 市值資料不足, 本次只用成交值排名")
        key = lambda c: (vr[c["code"]], 0)
    return sorted(base, key=key), use_cap


def pick_pool(ordered, prev_codes, n):
    pos = {c["code"]: i for i, c in enumerate(ordered)}
    keep = [c for c in ordered if c["code"] in prev_codes and pos[c["code"]] < n + BUFFER]
    keep = keep[:n]
    have = {c["code"] for c in keep}
    for c in ordered:
        if len(keep) >= n:
            break
        if c["code"] not in have:
            keep.append(c)
            have.add(c["code"])
    return sorted(keep, key=lambda c: pos[c["code"]])


# ---------- Google Sheet ----------
def truthy(v):
    return str(v).strip().upper() in ("TRUE", "1", "YES", "Y", "是")


def read_manual(ws):
    rows = ws.get_all_values()[1:] if ws else []
    out = []
    for r in rows:
        code = (r[0] if r else "").strip().lstrip("'")
        if not code or code.startswith("#"):
            continue
        name = r[1].strip() if len(r) > 1 else ""
        cat = r[2].strip() if len(r) > 2 and r[2].strip() else "自選"
        en = r[3].strip() if len(r) > 3 and r[3].strip() else "TRUE"
        out.append([code, name, cat, "TRUE" if truthy(en) else "FALSE"])
    return out


def main():
    stocks = load_market()
    cands = eligible(stocks)
    print("符合條件(科技產業, %g~%g 元, 成交值>=%g 億)的股票: %d 檔" % (
        MIN_PRICE, MAX_PRICE, MIN_TRADE_VALUE / 1e8, len(cands)))
    if len(cands) < MIN_POOL:
        print("符合條件的股票太少, 判定資料異常, 不更新。")
        sys.exit(1)

    import gspread
    info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    gc = gspread.service_account_from_dict(info)
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])

    def get_ws(title):
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            return None

    ws_watch = get_ws("Watchlist")
    if ws_watch is None:
        print("找不到 Watchlist 分頁, 不更新。")
        sys.exit(1)
    ws_manual = get_ws("ManualPicks")
    ws_auto = get_ws("AutoPool")
    prev = set(c.strip() for c in ws_auto.col_values(1)[1:]) if ws_auto else set()

    ordered, _ = rank_all(cands)
    pool = pick_pool(ordered, prev, POOL_SIZE)
    if len(pool) < min(MIN_POOL, POOL_SIZE):
        print("選出的股票太少, 不更新。")
        sys.exit(1)

    manual = read_manual(ws_manual)
    seen = {m[0] for m in manual}
    rows = [["stock_id", "name", "category", "enabled"]] + manual
    added = 0
    for c in pool:
        if c["code"] not in seen:
            rows.append([c["code"], c["name"], c["cat"], "TRUE"])
            added += 1

    now = datetime.now(TW).strftime("%Y-%m-%d %H:%M")
    print("\n今日選股池(%d 檔自動 + %d 檔手動):" % (added, len(manual)))
    new_codes = {c["code"] for c in pool}
    for i, c in enumerate(pool, 1):
        mark = "" if c["code"] in prev or not prev else "  <- 新進"
        cap = "%.0f" % (c["cap"] / 1e8) if c["cap"] else "-"
        print("%2d. %s %-8s %-6s 收盤 %8.2f  成交值 %6.1f 億  市值 %s 億%s" % (
            i, c["code"], c["name"], c["cat"], c["close"], c["value"] / 1e8, cap, mark))
    if prev:
        out = sorted(prev - new_codes)
        if out:
            print("\n今天移出: %s" % ", ".join(out))
    if manual:
        print("\n手動自選: %s" % ", ".join(m[0] for m in manual))

    if DRY_RUN:
        print("\n[試跑模式] 沒有寫入任何資料。")
        return

    # 備份原本的 Watchlist(只做一次)
    if get_ws("Watchlist_backup") is None:
        old = ws_watch.get_all_values()
        bak = sh.add_worksheet(title="Watchlist_backup",
                               rows=max(len(old) + 10, 50), cols=8)
        if old:
            bak.update(values=old, range_name="A1")
        print("已備份原本的 Watchlist 到 Watchlist_backup 分頁")

    # 第一次使用時建立 ManualPicks
    if ws_manual is None:
        wm = sh.add_worksheet(title="ManualPicks", rows=200, cols=8)
        wm.update(values=[["stock_id", "name", "category", "enabled"]], range_name="A1")
        wm.update(values=[["在這個分頁手動加自己挑的股票, 欄位格式同 Watchlist。程式每天會自動合併, 不會動這個分頁。"]],
                  range_name="F1")
        print("已建立 ManualPicks 分頁")

    ws_watch.clear()
    ws_watch.update(values=rows, range_name="A1", raw=False)

    auto_rows = [["stock_id", "name", "category", "close", "trade_value_100m",
                  "market_cap_100m", "updated_at"]]
    for c in pool:
        auto_rows.append([c["code"], c["name"], c["cat"], c["close"],
                          round(c["value"] / 1e8, 1),
                          round(c["cap"] / 1e8) if c["cap"] else "", now])
    if ws_auto is None:
        ws_auto = sh.add_worksheet(title="AutoPool", rows=100, cols=8)
    ws_auto.clear()
    ws_auto.update(values=auto_rows, range_name="A1", raw=False)
    print("\n已更新 Watchlist(共 %d 檔)與 AutoPool 分頁, 時間 %s" % (len(rows) - 1, now))


if __name__ == "__main__":
    main()
