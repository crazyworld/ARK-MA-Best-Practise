"""blueai-stats-aggregation-skill · lift_compare.py

3.1 活动拉升效果（纵向）：活动期 vs 基准期 的品牌声量 / 互动量 / NSR。
规则（KS_Metrics_Calculation v0.12）：
  - 主指标一律用【日均】（日均声量、日均互动量）；总量仅作辅助列示。活动期与基准期天数不同时，直接比总量会失真
  - 天数 = end_date - start_date + 1（含首尾）；不同天数时在 notes 写明
  - NSR = (正面-负面)/(正面+负面)，分母不含中性；变化量用百分点
  - 样本门槛：正面+负面合计 < 30 → 该期 NSR 标「样本不足」，不输出变化量
  - 基准期总声量 <= 100 → 整体标「基准期数据不足」，不输出变化率（见 KS_Exception_Handling）
  - 天数比 > 3 倍或 < 1/3 时附加提示（窗口差异过大，仅供参考）
输出 stats_brand_lift_{活动名}.xlsx；退出码 0 正常，2 输入错误
"""
import argparse, json, sys
from datetime import date
from pathlib import Path
from openpyxl import Workbook

MIN_BASELINE_VOLUME = 100
MIN_NSR_SAMPLE = 30
RATIO_WARN = 3.0


def load(p):
    return json.load(open(p, encoding='utf-8'))


def days_of(meta):
    s, e = date.fromisoformat(str(meta['start_date'])[:10]), date.fromisoformat(str(meta['end_date'])[:10])
    n = (e - s).days + 1
    if n <= 0:
        raise ValueError(f"日期区间非法：{s} ~ {e}")
    return n


def nsr_of(b):
    d = (b.get('sentiment_dist') or {}).get('_total') or {}
    pos, neg = int(d.get('positive', 0) or 0), int(d.get('negative', 0) or 0)
    return pos, neg


def pct(a, b):
    return None if not b else (a - b) / b * 100


def fmt_pct(v):
    return 'N/A' if v is None else f"{v:+.1f}%"


def compute(act, base):
    da, db = days_of(act['meta']), days_of(base['meta'])
    va, vb = int(act['total_volume']), int(base['total_volume'])
    ia, ib = int(act.get('total_interaction') or 0), int(base.get('total_interaction') or 0)
    notes = []
    if da != db:
        notes.append(f"活动期 {da} 天、基准期 {db} 天，窗口不等长：主指标采用日均对比，总量仅供参考")
    if max(da, db) / min(da, db) > RATIO_WARN:
        notes.append(f"两期天数相差超过 {RATIO_WARN:g} 倍，日均对比也仅供参考")

    rows = []
    if vb <= MIN_BASELINE_VOLUME:
        notes.append(f"基准期声量仅 {vb} 条（≤{MIN_BASELINE_VOLUME}），不具纵向可比性，不输出变化率")
        rows.append(['日均声量', round(va / da, 1), round(vb / db, 1), '基准期数据不足'])
        rows.append(['日均互动量', round(ia / da, 1), round(ib / db, 1), '基准期数据不足'])
    else:
        rows.append(['日均声量', round(va / da, 1), round(vb / db, 1), fmt_pct(pct(va / da, vb / db))])
        rows.append(['日均互动量', round(ia / da, 1), round(ib / db, 1), fmt_pct(pct(ia / da, ib / db))])

    pa, na = nsr_of(act); pb, nb = nsr_of(base)
    sa, sb = pa + na, pb + nb
    if sa < MIN_NSR_SAMPLE or sb < MIN_NSR_SAMPLE:
        who = '活动期' if sa < MIN_NSR_SAMPLE else '基准期'
        if sa < MIN_NSR_SAMPLE and sb < MIN_NSR_SAMPLE: who = '两期'
        rows.append(['品牌NSR', '样本不足' if sa < MIN_NSR_SAMPLE else f"{(pa-na)/sa*100:+.1f}%",
                     '样本不足' if sb < MIN_NSR_SAMPLE else f"{(pb-nb)/sb*100:+.1f}%", '样本不足'])
        notes.append(f"{who}正面+负面合计不足 {MIN_NSR_SAMPLE} 条，NSR 变化量不输出")
    else:
        na_v, nb_v = (pa - na) / sa * 100, (pb - nb) / sb * 100
        rows.append(['品牌NSR', f"{na_v:+.1f}%", f"{nb_v:+.1f}%", f"{na_v - nb_v:+.1f}pp"])

    total_rows = [['总声量（辅助）', va, vb, '总量受天数影响，勿直接比较'],
                  ['总互动量（辅助）', ia, ib, '总量受天数影响，勿直接比较']]
    return rows, total_rows, notes, (da, db)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--active', required=True, help='brand_stats.json（活动期）')
    p.add_argument('--baseline', required=True, help='brand_stats_baseline.json（基准期）')
    p.add_argument('--output', required=True)
    p.add_argument('--campaign', required=True)
    args = p.parse_args(argv)
    try:
        act, base = load(args.active), load(args.baseline)
        rows, total_rows, notes, (da, db) = compute(act, base)
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"❌ 输入错误：{e}", file=sys.stderr); return 2
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    wb = Workbook(); ws = wb.active
    ws.append(['指标', f'活动期（{da}天）', f'基准期（{db}天）', '变化'])
    for r in rows + total_rows: ws.append(r)
    ws.append([]); ws.append(['说明'])
    for n in notes: ws.append([n])
    f = out / f"stats_brand_lift_{args.campaign}.xlsx"; wb.save(f)
    print(f"  ✅ {f.name}", file=sys.stderr)
    for n in notes: print(f"⚠️ {n}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
