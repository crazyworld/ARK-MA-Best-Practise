"""blueai-stats-aggregation-skill · check_coverage.py

轮次二标注完成后、统计聚合之前运行：按「字段各自的适用范围」分别核对标注覆盖，
不允许用「正/负面任务的完成」代替「全范围标注」。

适用范围（与 KS_Data_Processing.md / annotation_prompt.md 一致）：
  情感(AI) / 是否活动相关(AI) : 全部行（无内容行标注为「标注跳过_*」除外）
  观点(AI)                    : 活动相关=是 且 情感∈{正面,负面}（该方向被判定不适用的除外）
  ugc_category                : 活动相关=是 且 内容类型=UGC（content_scope=仅PGC / 无UGC / D2 不适用时除外）
  discussion_dimension        : 活动相关=是（含中性）

退出码：0 通过（可含 WARN）；1 存在 ERROR（须补标后再进入 Step 7）；2 参数/数据错误
"""
import argparse, json, sys
from collections import Counter, defaultdict
import openpyxl

SENTIMENTS = ('正面', '中性', '负面')
SENTIMENT_WORDS = ['好评', '差评', '吐槽', '避雷', '失望', '满意', '不满', '喜欢', '种草', '安利', '夸赞',
                   '点赞', '批评', '抱怨', '推荐', '踩雷', '惊艳', '翻车', '正面', '负面', '积极', '消极']
EVAL_SUFFIX = ('好', '差', '棒', '赞', '烂', '高', '低', '贵', '便宜')
DIMENSIONS = ('品牌', '产品', '活动')


def read_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    hdrs = [str(c.value or '').strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = [dict(zip(hdrs, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    wb.close()
    return hdrs, rows


def s(v):
    return str(v if v is not None else '').strip()


def load_status(path):
    """读取 discovery 输出 JSON；返回 (status, categories_names)。文件未提供时返回 (None, None)。"""
    if not path:
        return None, None
    d = json.load(open(path, encoding='utf-8'))
    names = [c.get('name') for c in d.get('categories', []) if isinstance(c, dict)] if isinstance(d, dict) \
        else [c for c in d if isinstance(c, str)]
    return (d.get('status', 'ok') if isinstance(d, dict) else 'ok'), names


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True, help='annotated_data.xlsx（轮次二已完成）')
    p.add_argument('--round1-only', action='store_true', help='轮次一出口：只检查情感/相关性覆盖与全量空值其他指标，不要求轮次二字段')
    p.add_argument('--campaign-filter', help='分析对象；多活动数据必填（逐活动检查）')
    p.add_argument('--content-scope', default='全部', choices=['全部', '仅UGC', '仅PGC'])
    p.add_argument('--ugc-list', help='经 Validator 审核后的 ugc_category_list.json（D2输出）；status=not_applicable 时跳过 ugc 检查')
    p.add_argument('--opinion-positive', help='正面观点候选/审核 JSON；status=not_applicable 时跳过正面观点检查')
    p.add_argument('--opinion-negative', help='负面观点候选/审核 JSON；status=not_applicable 时跳过负面观点检查')
    p.add_argument('--other-warn', type=float, default=0.2, help='ugc_category=其他 占比超过该值给出 WARN')
    p.add_argument('--json-out')
    args = p.parse_args(argv)

    hdrs, rows = read_rows(args.input)
    # 观点列名：KS_Data_Processing 用「观点(AI)」，KS_Metadata_Structure 用「观点类别(AI)」，两者都接受
    OP = next((c for c in ('观点(AI)', '观点类别(AI)') if c in hdrs), '观点(AI)')
    need = ['情感(AI)', '是否活动相关(AI)', OP, 'ugc_category', 'discussion_dimension', '内容类型']
    if args.round1_only:
        need = ['情感(AI)', '是否活动相关(AI)']
    miss_cols = [c for c in need if c not in hdrs]
    if miss_cols:
        print(f"❌ annotated_data 缺列：{miss_cols}", file=sys.stderr)
        return 2

    objs = sorted({s(r.get('分析对象')) for r in rows} - {''})
    if args.campaign_filter:
        if objs and args.campaign_filter not in objs:
            print(f"❌ --campaign-filter「{args.campaign_filter}」不在分析对象中：{objs}", file=sys.stderr)
            return 2
        rows = [r for r in rows if s(r.get('分析对象')) in ('', args.campaign_filter)]
    elif len(objs) > 1:
        print(f"❌ 数据含 {len(objs)} 个分析对象 {objs}，请逐个活动用 --campaign-filter 检查", file=sys.stderr)
        return 2

    ugc_status, ugc_names = load_status(args.ugc_list)
    pos_status, pos_names = load_status(args.opinion_positive)
    neg_status, neg_names = load_status(args.opinion_negative)
    skip_op = {d for d, st in (('正面', pos_status), ('负面', neg_status)) if st == 'not_applicable'}

    findings = []          # (level, field, message)
    report = {}

    def add(level, field, msg):
        findings.append((level, field, msg))

    # —— 类别定义边界：观点=讨论话题、参与方式=动作/内容形式，名称不得带情感/评价倾向 ——
    for label, names in (('参与方式', ugc_names), ('正面观点', pos_names), ('负面观点', neg_names)):
        for n in names or []:
            n = str(n or '')
            hit = [w for w in SENTIMENT_WORDS if w in n]
            if hit or n.endswith(EVAL_SUFFIX):
                add('WARN', f'{label}类别', f"「{n}」含情感/评价措辞{hit or ''}，类别应只描述"
                    f"{'动作与内容形式' if label == '参与方式' else '讨论的话题'}（情感由独立字段承载）")

    # —— 轮次一 ——
    valid = []
    skipped = 0
    for r in rows:
        if s(r.get('情感(AI)')).startswith('标注跳过'):
            skipped += 1
            continue
        valid.append(r)
    bad1 = [r for r in valid if s(r.get('情感(AI)')) not in SENTIMENTS or s(r.get('是否活动相关(AI)')) not in ('是', '否')]
    report['round1'] = {"rows": len(rows), "skipped_no_content": skipped, "invalid": len(bad1)}
    if bad1:
        add('ERROR', '情感/是否活动相关', f"{len(bad1)} 行取值缺失或非法（应为 正面/中性/负面 与 是/否）")

    # Round-one denominators include rows labelled unrelated; accuracy is reviewed separately.
    report['round1_quality'] = {}
    for field in ('情感(AI)', '是否活动相关(AI)'):
        n1 = len(valid)
        empty = sum(not s(r.get(field)) for r in valid)
        other = sum(s(r.get(field)) == '其他' for r in valid)
        count = empty + other
        report['round1_quality'][field] = {
            'scope': n1, 'planned_sample_size': (n1 + 9) // 10,
            'empty': empty, 'other': other, 'empty_or_other': count,
            'empty_or_other_share': count / n1 if n1 else None,
            'below_minimum_limit': count * 10 < n1 if n1 else None,
            'meets_target': count * 20 < n1 if n1 else None,
            'semantic_accuracy': 'requires_independent_Validator_review'}

    if args.round1_only:
        print(json.dumps({'report': report, 'findings': findings}, ensure_ascii=False, indent=2))
        if args.json_out:
            with open(args.json_out, 'w', encoding='utf-8') as result_file:
                json.dump({'report': report, 'findings': findings}, result_file, ensure_ascii=False, indent=2)
        return 1 if any(level == 'ERROR' for level, _, _ in findings) else 0

    rel = [r for r in valid if s(r.get('是否活动相关(AI)')) == '是']

    # —— 观点(AI) ——
    op_scope = [r for r in rel if s(r.get('情感(AI)')) in ('正面', '负面') and s(r.get('情感(AI)')) not in skip_op]
    op_miss = [r for r in op_scope if not s(r.get(OP))]
    by_dir = Counter(s(r.get('情感(AI)')) for r in op_miss)
    report['opinion'] = {"scope": len(op_scope), "missing": len(op_miss), "missing_by_sentiment": dict(by_dir),
                         "direction_not_applicable": sorted(skip_op)}
    if op_miss:
        add('ERROR', '观点(AI)', f"适用范围 {len(op_scope)} 行中 {len(op_miss)} 行为空（按情感：{dict(by_dir)}）")
    neu_blank = [r for r in rel if s(r.get('情感(AI)')) == '中性' and not s(r.get(OP))]
    if neu_blank:
        add('WARN', '观点(AI)', f"{len(neu_blank)} 行中性内容观点为空（KS 规定填「无」）")
    skipped_dir_blank = [r for r in rel if s(r.get('情感(AI)')) in skip_op and not s(r.get(OP))]
    if skipped_dir_blank:
        add('WARN', '观点(AI)', f"{len(skipped_dir_blank)} 行属于被判定不适用的方向（{sorted(skip_op)}），建议填「无」")

    # Confirmed whole-population opinion empty/other metric; per post, not per label.
    def tokens(value):
        return [part.strip() for part in str(value or '').replace('，', ',').split(',') if part.strip()]

    empty_count = sum(not s(r.get(OP)) for r in op_scope)
    other_count = sum('其他' in tokens(r.get(OP)) for r in op_scope)
    combined_count = sum(not s(r.get(OP)) or '其他' in tokens(r.get(OP)) for r in op_scope)
    n = len(op_scope)
    report['opinion_quality'] = {
        'applicable': bool(n), 'scope': n, 'planned_sample_size': (n + 9) // 10,
        'empty': empty_count, 'other': other_count, 'empty_or_other': combined_count,
        'empty_or_other_share': combined_count / n if n else None,
        'below_minimum_limit': combined_count * 10 < n if n else None,
        'meets_target': combined_count * 20 < n if n else None,
        'by_sentiment': {}, 'semantic_accuracy': 'requires_independent_Validator_review'}
    for direction in ('正面', '负面'):
        group = [r for r in op_scope if s(r.get('情感(AI)')) == direction]
        count = sum(not s(r.get(OP)) or '其他' in tokens(r.get(OP)) for r in group)
        report['opinion_quality']['by_sentiment'][direction] = {
            'scope': len(group), 'empty_or_other': count,
            'empty_or_other_share': count / len(group) if group else None}
    if n and combined_count * 10 >= n:
        add('ERROR', '观点空值/其他', f"合计 {combined_count}/{n}（{combined_count/n:.1%}），要求低于10%")
    elif n and combined_count * 20 >= n:
        add('WARN', '观点空值/其他', f"合计 {combined_count}/{n}，已低于10%但未达到低于5%的目标")
    illegal = [r for r in op_scope if any(t in ('无', 'N/A', 'NA', 'null', 'None') for t in tokens(r.get(OP)))]
    if illegal:
        add('ERROR', '观点非法占位', f"{len(illegal)}条应标注观点帖子含无/N/A等非法占位，须补标")

    # —— ugc_category ——
    ugc_applicable = args.content_scope != '仅PGC' and ugc_status != 'not_applicable'
    ugc_scope = [r for r in rel if s(r.get('内容类型')) == 'UGC']
    if not ugc_applicable or not ugc_scope:
        report['ugc_category'] = {"applicable": False,
                                  "reason": "content_scope=仅PGC" if args.content_scope == '仅PGC'
                                  else ("D2 判定不适用" if ugc_status == 'not_applicable' else "活动相关 UGC 为 0")}
        stray = [r for r in rel if s(r.get('ugc_category')) not in ('', 'N/A')]
        if stray:
            add('WARN', 'ugc_category', f"不适用场景下仍有 {len(stray)} 行被标了类别，应为 N/A")
    else:
        u_miss = [r for r in ugc_scope if s(r.get('ugc_category')) in ('', 'N/A')]
        u_by = Counter(s(r.get('情感(AI)')) for r in u_miss)
        dist = Counter(s(r.get('ugc_category')) for r in ugc_scope if s(r.get('ugc_category')) not in ('', 'N/A'))
        other_share = dist.get('其他', 0) / len(ugc_scope)
        report['ugc_category'] = {"applicable": True, "scope": len(ugc_scope), "missing": len(u_miss),
                                  "missing_by_sentiment": dict(u_by), "other_share": round(other_share, 3)}
        if u_miss:
            hint = ''
            if u_by and set(u_by) == {'中性'}:
                hint = '（缺失全部为中性行：疑似 ugc_category 被并入正/负面观点任务，中性行未标注）'
            add('ERROR', 'ugc_category', f"适用范围 {len(ugc_scope)} 行中 {len(u_miss)} 行为空或 N/A（按情感：{dict(u_by)}）{hint}")
        if other_share > args.other_warn:
            add('WARN', 'ugc_category',
                f"「其他」占 {other_share:.0%} 超过 {args.other_warn:.0%}：类别发现可能遗漏了低互动的参与方式，建议扩大/混合抽样后重跑 D2")
        if ugc_names:
            off = [v for v in dist if v != '其他' and v not in ugc_names]
            if off:
                add('WARN', 'ugc_category', f"出现不在类别列表中的取值：{off}")
        out_scope = [r for r in rel if s(r.get('内容类型')) != 'UGC' and s(r.get('ugc_category')) not in ('', 'N/A')]
        if out_scope:
            add('WARN', 'ugc_category', f"{len(out_scope)} 行非 UGC 却被标了类别，应为 N/A")

    # —— discussion_dimension（含中性）——
    d_miss = [r for r in rel if s(r.get('discussion_dimension')) not in DIMENSIONS]
    d_by = Counter(s(r.get('情感(AI)')) for r in d_miss)
    report['discussion_dimension'] = {"scope": len(rel), "missing": len(d_miss), "missing_by_sentiment": dict(d_by)}
    if d_miss:
        hint = ''
        if d_by and set(d_by) == {'中性'}:
            hint = '（缺失全部为中性行：疑似 discussion_dimension 被并入正/负面任务）'
        add('ERROR', 'discussion_dimension', f"适用范围 {len(rel)} 行中 {len(d_miss)} 行取值缺失/N/A/非法（按情感：{dict(d_by)}）{hint}")

    # —— 输出 ——
    print(f"== 轮次二覆盖检查（{args.campaign_filter or '全部数据'}）==")
    print(f"总行数 {len(rows)}｜无内容跳过 {skipped}｜活动相关 {len(rel)}")
    for k, v in report.items():
        print(f"  {k}: {v}")
    errs = [f for f in findings if f[0] == 'ERROR']
    for lv, fld, msg in findings:
        print(f"[{lv}] {fld}: {msg}")
    print("✅ 覆盖检查通过" if not errs else f"❌ {len(errs)} 项 ERROR：请补标后再进入 Step 7（不得让正/负面任务代替全范围标注）")
    if args.json_out:
        json.dump({"report": report, "findings": findings}, open(args.json_out, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
