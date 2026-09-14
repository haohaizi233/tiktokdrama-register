# -*- coding: utf-8 -*-
"""Scrape SAMR standardized scope items from jyfwyun.com."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "glossary" / "scope_catalog.json"
API = "https://api.jyfwyun.com/cloud-service/cross/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.jyfwyun.com/",
    "Origin": "https://www.jyfwyun.com",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}
# Empty-keyword listing is region-filtered. Union several provinces to approach the
# nationwide SAMR directory. pageSize=20 is honored; 50/100 fall back to 10.
ACS = [
    "110000", "120000", "310000", "320000", "330000", "370000",
    "410000", "420000", "440000", "500000", "510000", "610000", "650000",
]
KEYWORDS = [
    "销售", "服务", "建筑", "食品", "工程", "批发", "零售", "电子产品", "咨询", "进出口",
    "汽车", "机械设备", "设计", "口罩", "计算机", "制造", "生产", "种植", "养殖", "加工",
    "维修", "租赁", "运输", "仓储", "软件", "技术", "广告", "教育", "医疗", "环保",
    "能源", "化工", "金属", "塑料", "纺织", "服装", "家具", "建材", "化妆品", "农产品",
    "中药材", "再生", "回收", "劳务", "家政", "珠宝", "酒", "餐饮", "互联网", "信息",
    "仪器", "电器", "电力", "燃气", "水", "道路", "货物", "装卸", "园林", "装饰",
    "涂料", "橡胶", "玻璃", "陶瓷", "木材", "纸", "印刷", "包装", "医药", "器械",
    "宠物", "饲料", "种子", "化肥", "农药", "煤炭", "矿", "石材", "水泥", "钢材",
    "阀门", "轴承", "模具", "五金", "灯具", "日用", "百货", "玩具", "文具",
    "图书", "音像", "通讯", "通信", "半导体", "光伏", "电池", "储能", "充电", "二手",
    "人力资源", "劳务派遣", "物业管理", "房地产", "融资", "保险", "证券", "会计", "审计",
    "翻译", "会议", "展览", "旅游", "住宿", "洗染", "清洁", "保安", "殡葬", "修理",
    "摩托车", "船舶", "铁路", "航空", "核", "烟草", "出版物", "成品油", "燃气",
    "特种设备", "危险化学品", "医疗器械", "药品", "兽药", "疫苗", "食品添加剂",
    "学前", "培训", "广播", "电影", "演出", "文物", "测绘", "地质", "气象",
    "银行", "小额贷款", "典当", "拍卖", "担保", "基金", "期货", "信托",
]


def search(keyword: str, page: int, ac: str, page_size: int) -> dict:
    payload = {
        "keyword": keyword,
        "pageNum": page,
        "pageSize": page_size,
        "ac": ac,
    }
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode("utf-8"), headers=HEADERS, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load() -> dict[str, dict]:
    if OUT.exists():
        return json.loads(OUT.read_text(encoding="utf-8"))
    return {}


def save(catalog: dict[str, dict]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ingest(catalog: dict[str, dict], records: list) -> int:
    added = 0
    for rec in records:
        item = (rec.get("standardItem") or "").replace(" ", "").strip()
        if not item:
            continue
        row = {
            "scope_code": rec.get("scopeCode") or "",
            "gb": rec.get("gbName") or "",
            "permit": rec.get("permitType"),
        }
        if item in catalog:
            old = catalog[item]
            if not old.get("scope_code") and row["scope_code"]:
                old["scope_code"] = row["scope_code"]
            if not old.get("gb") and row["gb"]:
                old["gb"] = row["gb"]
            if old.get("permit") is None and row["permit"] is not None:
                old["permit"] = row["permit"]
            continue
        catalog[item] = row
        added += 1
    return added


def crawl(catalog: dict[str, dict], keyword: str, ac: str, page_size: int, max_pages: int) -> None:
    page = 1
    while page <= max_pages:
        try:
            data = search(keyword, page, ac, page_size)
        except Exception as exc:
            print(f"  retry {ac} {keyword or '∅'} p{page} {exc}", flush=True)
            time.sleep(1.5)
            try:
                data = search(keyword, page, ac, page_size)
            except Exception as exc2:
                print(f"  fail {ac} {keyword or '∅'} p{page} {exc2}", flush=True)
                break
        if data.get("code") != 0:
            print(f"  err {ac} {keyword or '∅'} p{page} {data.get('msg')}", flush=True)
            break
        result = (data.get("result") or {}).get("data") or {}
        records = result.get("records") or []
        pages = int(result.get("pages") or 1)
        added = ingest(catalog, records)
        print(
            f"  {ac} {keyword or '∅'} p{page}/{pages} +{added} total={len(catalog)}",
            flush=True,
        )
        if page >= pages or not records:
            break
        page += 1
        time.sleep(0.12)
        if page % 5 == 0:
            save(catalog)
    save(catalog)


def main() -> None:
    catalog = load()
    print(f"already {len(catalog)}", flush=True)
    for ac in ACS:
        crawl(catalog, "", ac, 20, 250)
    save(catalog)
    print(f"wrote {OUT} items={len(catalog)}", flush=True)


if __name__ == "__main__":
    main()
