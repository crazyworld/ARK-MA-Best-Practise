"""活动拉升效果（FIX-15）：活动期 vs 基准期。

唯一依据：原版 `blueai-stats-aggregation-skill/scripts/lift_compare.py` 与
`KS_Exception_Handling.md:56-95`（基准期落在节假日高峰月）。

口径（原文硬性要求）：
  - 主指标一律用【日均】（日均声量、日均互动量）；总量仅作辅助列示。
  - 天数 = end - start + 1（含首尾），两期各用自己的实际天数，不写死 30。
  - NSR = (正面-负面)/(正面+负面)，分母不含中性；变化量用百分点 pp。
  - 正面+负面合计 < 30 → 该期 NSR 样本不足，不输出变化量。
  - 基准期总声量 <= 100 → 不输出变化率，提示延长基准期。
  - 两期天数相差 > 3 倍 → 附加「仅供参考」。
  - 基准期声量或互动显著高于活动期（>20%）且基准期含节假日 → 用固定措辞，
    图注加「⚠ 基准期含{节日}，声量基数高于日常水平」。
"""
from __future__ import annotations

from datetime import date, datetime

MIN_BASELINE_VOLUME = 100
MIN_NSR_SAMPLE = 30
RATIO_WARN = 3.0
HOLIDAY_GAP_THRESHOLD = 0.20

# 节假日区间（KS_Exception_Handling.md:58）：按年份列出，覆盖 2024-2027。
# 春节跨年，按实际春节假期区间登记；区间外的年份 holiday_check=unknown。
HOLIDAYS: dict[int, list[tuple[str, str, str]]] = {
    2024: [("元旦", "2024-01-01", "2024-01-01"),
           ("春节", "2024-02-10", "2024-02-17"),
           ("五一", "2024-05-01", "2024-05-07"),
           ("618", "2024-06-01", "2024-06-20"),
           ("国庆", "2024-10-01", "2024-10-07"),
           ("双十一", "2024-11-01", "2024-11-15")],
    2025: [("元旦", "2025-01-01", "2025-01-01"),
           ("春节", "2025-01-28", "2025-02-04"),
           ("五一", "2025-05-01", "2025-05-07"),
           ("618", "2025-06-01", "2025-06-20"),
           ("国庆", "2025-10-01", "2025-10-07"),
           ("双十一", "2025-11-01", "2025-11-15")],
    2026: [("元旦", "2026-01-01", "2026-01-01"),
           ("春节", "2026-02-17", "2026-02-23"),
           ("五一", "2026-05-01", "2026-05-07"),
           ("618", "2026-06-01", "2026-06-20"),
           ("国庆", "2026-10-01", "2026-10-07"),
           ("双十一", "2026-11-01", "2026-11-15")],
    2027: [("元旦", "2027-01-01", "2027-01-01"),
           ("春节", "2027-02-06", "2027-02-12"),
           ("五一", "2027-05-01", "2027-05-07"),
           ("618", "2027-06-01", "2027-06-20"),
           ("国庆", "2027-10-01", "2027-10-07"),
           ("双十一", "2027-11-01", "2027-11-15")],
}


def _ymd(value):
    """接受 'YYYY-MM-DD' / 'YYYYMMDD' / date / datetime / epoch ms。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).date()
    text = str(value).strip().replace("-", "").replace("/", "")[:8]
    if len(text) != 8 or not text.isdigit():
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def days_inclusive(start, end) -> int | None:
    s, e = _ymd(start), _ymd(end)
    if not s or not e:
        return None
    n = (e - s).days + 1
    return n if n > 0 else None


def _meta_of(bundle: dict) -> dict:
    b = bundle or {}
    raw_meta = b.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    return {
        "start": meta.get("start_date") or meta.get("start") or b.get("start_date"),
        "end": meta.get("end_date") or meta.get("end") or b.get("end_date"),
    }


def _sentiment_of(bundle: dict) -> tuple[int, int]:
    b = bundle or {}
    dist = b.get("sentiment_dist") or {}
    if "_total" in dist and isinstance(dist["_total"], dict):
        dist = dist["_total"]
    return int(dist.get("positive", 0) or 0), int(dist.get("negative", 0) or 0)


def holidays_in(start, end) -> tuple[list[str], str]:
    """返回 (命中的节假日名, holiday_check)。区间跨越未登记年份时 unknown。"""
    s, e = _ymd(start), _ymd(end)
    if not s or not e:
        return [], "unknown"
    years = range(s.year, e.year + 1)
    unknown = any(year not in HOLIDAYS for year in years)
    found = []
    for year in years:
        for name, hs, he in HOLIDAYS.get(year, []):
            h_start, h_end = _ymd(hs), _ymd(he)
            if h_start and h_end and h_start <= e and h_end >= s:
                if name not in found:
                    found.append(name)
    return found, ("unknown" if unknown else "ok")


def compare(period: dict, base: dict) -> dict:
    """比较两期。period/base 各自带 start/end/total_volume/interaction/sentiment。"""
    period, base = period or {}, base or {}
    d_act = days_inclusive(period.get("start"), period.get("end"))
    d_base = days_inclusive(base.get("start"), base.get("end"))
    v_act, v_base = int(period.get("volume") or 0), int(base.get("volume") or 0)
    i_act, i_base = int(period.get("interaction") or 0), int(base.get("interaction") or 0)

    out = {
        "status": "available",
        "days": {"period": d_act, "base": d_base},
        "totals": {
            "period": {"volume": v_act, "interaction": i_act},
            "base": {"volume": v_base, "interaction": i_base},
        },
        "daily": {
            "period": {"volume": round(v_act / d_act, 1) if d_act else None,
                       "interaction": round(i_act / d_act, 1) if d_act else None},
            "base": {"volume": round(v_base / d_base, 1) if d_base else None,
                     "interaction": round(i_base / d_base, 1) if d_base else None},
        },
        "daily_volume_change_pct": None,
        "daily_interaction_change_pct": None,
        "volume_change": None,          # 总量差，仅辅助，不得作为结论
        "nsr_change_pp": None,
        "base_insufficient": False,
        "nsr_insufficient": False,
        "days_ratio_flag": False,
        "holiday_flags": [],
        "holiday_check": "unknown",
        "holiday_text": None,
        "notes": [],
    }
    if not d_act or not d_base:
        out["status"] = "invalid_period"
        out["notes"].append("活动期或基准期日期缺失/非法，无法计算天数")
        return out

    out["volume_change"] = v_act - v_base
    if d_act != d_base:
        out["notes"].append(
            f"活动期 {d_act} 天、基准期 {d_base} 天，窗口不等长："
            "主指标采用日均对比，总量仅供参考")
    if max(d_act, d_base) / min(d_act, d_base) > RATIO_WARN:
        out["days_ratio_flag"] = True
        out["notes"].append(f"两期天数相差超过 {RATIO_WARN:g} 倍，日均对比也仅供参考")

    if v_base <= MIN_BASELINE_VOLUME:
        # 基准期总量过小 → 不输出变化率（KS_Exception_Handling）。
        out["base_insufficient"] = True
        out["notes"].append(
            f"基准期声量仅 {v_base} 条（≤{MIN_BASELINE_VOLUME}），"
            "不具纵向可比性，不输出变化率；建议延长基准期或跳过对比")
    else:
        out["daily_volume_change_pct"] = round(
            (v_act / d_act - v_base / d_base) / (v_base / d_base) * 100, 1)
        if i_base:
            out["daily_interaction_change_pct"] = round(
                (i_act / d_act - i_base / d_base) / (i_base / d_base) * 100, 1)

    p_pos, p_neg = _sentiment_of(period)
    b_pos, b_neg = _sentiment_of(base)
    p_denom, b_denom = p_pos + p_neg, b_pos + b_neg
    out["nsr"] = {
        "period": round((p_pos - p_neg) / p_denom * 100, 1) if p_denom else None,
        "base": round((b_pos - b_neg) / b_denom * 100, 1) if b_denom else None,
        "sample": {"period": p_denom, "base": b_denom},
    }
    if p_denom < MIN_NSR_SAMPLE or b_denom < MIN_NSR_SAMPLE:
        out["nsr_insufficient"] = True
        who = ("两期" if p_denom < MIN_NSR_SAMPLE and b_denom < MIN_NSR_SAMPLE
               else ("活动期" if p_denom < MIN_NSR_SAMPLE else "基准期"))
        out["notes"].append(
            f"{who}正面+负面合计不足 {MIN_NSR_SAMPLE} 条，NSR 变化量不输出")
    else:
        out["nsr_change_pp"] = round(out["nsr"]["period"] - out["nsr"]["base"], 1)

    # 节假日：基准期落在节假日高峰月，且两期总量差 >20%。
    flags, check = holidays_in(base.get("start"), base.get("end"))
    out["holiday_flags"], out["holiday_check"] = flags, check
    if check == "unknown":
        out["notes"].append("基准期超出已登记节假日年份范围，节假日影响未核查")
    elif flags:
        gap = (abs(v_base - v_act) / v_act) if v_act else None
        if gap is not None and gap > HOLIDAY_GAP_THRESHOLD:
            out["holiday_text"] = (
                f"基准期（{base.get('start')}～{base.get('end')}）声量 {v_base:,} 条，"
                f"受{'、'.join(flags)}影响，月度声量处于年内自然高峰，"
                "两期不在同等可比基准上，建议以日均声量作为参考")
            out["notes"].append(
                f"⚠ 基准期含{'、'.join(flags)}，声量基数高于日常水平")
    return out


def from_bundles(period_bundle: dict, base_bundle: dict) -> dict:
    """从 brand_stats / brand_stats_baseline 两包直接算（SSJH 返回值结构）。"""
    def pack(bundle):
        meta = _meta_of(bundle)
        b = bundle or {}
        return {
            "start": meta["start"], "end": meta["end"],
            "volume": b.get("total_volume") or b.get("row_count") or 0,
            "interaction": b.get("total_interaction") or 0,
            "sentiment_dist": b.get("sentiment_dist") or {},
        }
    return compare(pack(period_bundle), pack(base_bundle))
