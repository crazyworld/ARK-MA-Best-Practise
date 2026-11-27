"""blueai-phase-ugc-discovery-skill · run.py

从「活动相关 UGC」中发现参与方式类别。
- 多活动：必须用 --campaign-filter 指定分析对象（检测到多个分析对象而未指定时报错，避免混池抽样）
- 抽样：默认 mixed（互动 Top 50% + 其余随机 50%，固定 seed 可复现），避免只看高互动帖而漏掉低互动的参与方式
- 不适用：仅PGC / 无UGC / UGC过少 → status=not_applicable，退出码 0，hc_required=false（不需要用户确认）
"""
import argparse, json, random, re, sys
import openpyxl, anthropic
from pathlib import Path

# 类别名称中不应出现的情感/倾向措辞（情感由独立字段承载；观点=讨论话题，参与方式=动作/内容形式）
SENTIMENT_WORDS = ['好评', '差评', '吐槽', '避雷', '失望', '满意', '不满', '喜欢', '种草', '安利', '夸赞',
                   '点赞', '批评', '抱怨', '推荐', '踩雷', '惊艳', '翻车', '正面', '负面', '积极', '消极']
CATCHALL_NAMES = ('其他', '其它', '未分类', '杂项', '无法归类', '其余')
EVAL_SUFFIX = re.compile(r'(好|差|棒|赞|烂|高|低|贵|便宜)$')


def lint_names(cats, kind):
    """返回告警列表：名称含情感/评价措辞，或名称重复。仅告警，不中断。"""
    warns, seen = [], set()
    for c in cats:
        n = str(c.get('name', '')).strip()
        hit = [w for w in SENTIMENT_WORDS if w in n]
        if hit or EVAL_SUFFIX.search(n):
            warns.append(f"类别「{n}」含情感/评价措辞{hit or ['（以好/差/高/低等评价词结尾）']}："
                         f"{kind}应只描述{'讨论的话题' if kind == '观点' else '用户做了什么动作、采用什么内容形式'}，请提交 Validator 核查后改名")
        if n in CATCHALL_NAMES:
            warns.append(f"类别「{n}」是兜底类：不应单设，无法归类的内容由标注环节统一填「其他」，请提交 Validator 核查后删除")
        if n in seen:
            warns.append(f"类别名称重复：「{n}」")
        seen.add(n)
    return warns

def parse_categories(raw):
    """宽松解析模型输出；失败返回 None（调用方重试）。"""
    for cand in (raw, (re.search(r'\{.*\}', raw, re.DOTALL) or [None])[0] if re.search(r'\{.*\}', raw, re.DOTALL) else None):
        if not cand:
            continue
        try:
            d = json.loads(cand)
            cats = d.get('categories') if isinstance(d, dict) else None
            if cats:
                return cats
        except Exception:
            continue
    return None


def call_with_retry(client, model, prompt, max_tokens, tries=3):
    """调用模型并解析类别；解析失败自动重试（追加严格 JSON 提示），仍失败返回 (None, 最后一次原文)。"""
    raw = ''
    for i in range(tries):
        p = prompt if i == 0 else prompt + "\n\n注意：上一次输出不是合法 JSON。只输出一个合法的 JSON 对象，字符串内的引号请用中文引号或转义，不要附加任何说明文字。"
        msg = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": p}])
        raw = next((b.text for b in msg.content if hasattr(b, 'text')), '').strip()
        cats = parse_categories(raw)
        if cats:
            return cats, raw
        print(f"⚠️ 第 {i + 1}/{tries} 次未得到合法 JSON，重试" if i < tries - 1 else f"⚠️ 第 {i + 1}/{tries} 次仍未得到合法 JSON", file=sys.stderr)
    return None, raw


def lint_count(n, rng, kind):
    """类别数量超出要求区间时告警（模型常不遵守数量要求）。rng 形如 '5-8'。"""
    try:
        lo, hi = [int(x) for x in str(rng).split('-')]
    except ValueError:
        return []
    if n < lo or n > hi:
        return [f"{kind}类别数为 {n}，要求 {lo}-{hi}：请提交 Validator 核查是否需要合并相近类别（如边界交叉的类）或补充遗漏类别"]
    return []


def to_int(v):
    try:
        return int(float(str(v).replace(',', ''))) if v not in (None, '') else 0
    except ValueError:
        return 0


def read_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    hdrs = [str(c.value or '').strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = [dict(dict(zip(hdrs, [c.value for c in r])), _source_excel_row=i)
            for i, r in enumerate(ws.iter_rows(min_row=2), 2)]
    wb.close()
    return rows


def filter_campaign(rows, cf, allow_mixed):
    """按「分析对象」过滤；口径与 aggregate.py 一致（分析对象为空的行保留）。"""
    objs = sorted({str(r.get('分析对象', '') or '').strip() for r in rows} - {''})
    if cf:
        if objs and cf not in objs:
            print(f"❌ --campaign-filter「{cf}」不在数据的分析对象中：{objs}", file=sys.stderr)
            sys.exit(2)
        rows = [r for r in rows if str(r.get('分析对象', '') or '').strip() in ('', cf)]
    elif len(objs) > 1 and not allow_mixed:
        print(f"❌ 数据含 {len(objs)} 个分析对象 {objs}，请逐个活动用 --campaign-filter 运行，"
              f"否则不同活动的 UGC 会混池抽样（确需混合请加 --allow-mixed）", file=sys.stderr)
        sys.exit(2)
    return rows


def draw_sample(pool, n, mode, top_ratio, seed):
    ranked = sorted(pool, key=lambda x: to_int(x.get('总互动量')), reverse=True)
    if len(ranked) <= n:
        return ranked, {"mode": "all", "top_n": len(ranked), "random_n": 0, "seed": None}
    if mode == 'top':
        return ranked[:n], {"mode": "top", "top_n": n, "random_n": 0, "seed": None}
    top_n = int(round(n * top_ratio))
    rest = ranked[top_n:]
    rnd = random.Random(seed).sample(rest, n - top_n)
    return ranked[:top_n] + rnd, {"mode": "mixed", "top_n": top_n, "random_n": n - top_n, "seed": seed}


def write_result(path, result):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def not_applicable(args, code, reason, pool_size):
    result = {"status": "not_applicable", "hc_required": False, "validator_required": False, "review_status": "not_applicable", "reason_code": code, "reason": reason,
              "campaign_name": args.campaign_name, "campaign_filter": args.campaign_filter,
              "pool_size": pool_size, "categories": []}
    print(f"ℹ️ 不适用：{reason}（无需人工确认，记录不适用原因；报告 2.N.4② 按 KS 注明原因）", file=sys.stderr)
    write_result(args.output, result)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--campaign-name', required=True)
    p.add_argument('--campaign-filter', help='分析对象取值；多活动数据必填（逐活动运行）')
    p.add_argument('--allow-mixed', action='store_true', help='允许多个分析对象混合抽样（不推荐）')
    p.add_argument('--content-scope', default='全部', choices=['全部', '仅UGC', '仅PGC'],
                   help='取自需求确认单 content_scope；仅PGC 时直接判定不适用')
    p.add_argument('--sample-size', type=int, default=150)
    p.add_argument('--sampling', default='mixed', choices=['mixed', 'top'])
    p.add_argument('--top-ratio', type=float, default=0.5, help='mixed 模式下按互动取 Top 的比例')
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--min-posts', type=int, default=10, help='活动相关 UGC 少于该值判定不适用')
    p.add_argument('--model', default='claude-haiku-4-5-20251001')
    args = p.parse_args(argv)

    if args.content_scope == '仅PGC':
        return not_applicable(args, 'pgc_only_scope', 'content_scope=仅PGC，本活动无 UGC 参与方式分析', 0)

    rows = filter_campaign(read_rows(args.input), args.campaign_filter, args.allow_mixed)
    pool = [r for r in rows
            if str(r.get('是否活动相关(AI)', r.get('是否活动相关', '')) or '').strip() == '是'
            and str(r.get('内容类型', '') or '').strip() == 'UGC']
    print(f"活动相关 UGC: {len(pool)} 条", file=sys.stderr)

    if len(pool) == 0:
        return not_applicable(args, 'no_ugc', '活动相关 UGC 为 0 条', 0)
    if len(pool) < args.min_posts:
        return not_applicable(args, 'too_few_ugc',
                              f'活动相关 UGC 仅 {len(pool)} 条，不足 {args.min_posts} 条，不做参与方式分类', len(pool))

    posts, sampling = draw_sample(pool, args.sample_size, args.sampling, args.top_ratio, args.seed)
    print(f"抽样 {len(posts)}/{len(pool)} 条（{sampling}）", file=sys.stderr)

    lines = '\n'.join(
        f"{i}. [互动:{to_int(r.get('总互动量'))}] "
        f"{(str(r.get('标题', '') or '') + ' ' + str(r.get('内容', '') or '')).strip()[:180]}"
        for i, r in enumerate(posts, 1)
    )
    prompt = (
        f"阅读以下「{args.campaign_name}」活动的UGC样本，识别用户参与该活动的主要方式，"
        f"归纳3-6个类别。\n\n"
        f"## 类别的定义（必须遵守）\n"
        f"- 参与方式 = 用户【做了什么动作】、【采用什么内容形式】（如：到店打卡、开箱测评、晒单分享、二创改编、话题接龙、提问求助），"
        f"类别名称2-4字，用动作或内容形式命名\n"
        f"- 类别可以是动作（到店打卡、二创改编）或内容形式（开箱测评、晒单分享），两者可在同一套类别中并列；但类别之间不得重叠——一条帖子同时符合多类时，按其主要参与方式归入一类，并在判断标准里写清区分点；类别中不要混入话题、情感或人群维度\n"
        f"- 不得按情感、态度或观点倾向分类，类别名称与定义中都不要出现好评、吐槽、避雷、种草、安利、喜欢、失望等倾向词；"
        f"同样的动作和形式，无论内容夸还是骂，归入同一类（情感另有独立字段）\n\n"
        f"## MECE 要求\n"
        f"- 互斥：任取两类，应能用一句话说清区别；一条帖子按其主要参与方式只归入一类\n"
        f"- 穷尽：类别合起来应覆盖样本中绝大多数内容，个别无法归类的留给‘其他’，不要单独设‘其他’类\n"
        f"- 每类配一句判断标准和2条示例原文；输出前自查是否满足以上要求\n\n"
        f"样本包含高互动帖和随机抽取的帖子两部分，请同等对待，不要只关注高互动帖；"
        f"低频但边界清晰的参与方式也应保留。输出纯JSON不加代码块。\n\n"
        f"## UGC样本\n{lines}\n\n"
        f'## 输出格式\n{{"categories":[{{"name":"","definition":"","examples":["",""]}}]}}'
    )

    client = anthropic.Anthropic()
    cats, raw = call_with_retry(client, args.model, prompt, 2000)
    if not cats:
        dump = Path(args.output).with_suffix('.raw.txt')
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(raw, encoding='utf-8')
        print(f"❌ 模型多次未返回有效类别（原文已存 {dump}），请重试或调大 --sample-size（不会写出空列表进入 validate）", file=sys.stderr)
        sys.exit(3)

    warns = lint_names(cats, '参与方式') + lint_count(len(cats), '3-6', '参与方式')
    result = {"status": "ok", "hc_required": False, "validator_required": True, "review_status": "pending", "campaign_name": args.campaign_name,
              "campaign_filter": args.campaign_filter, "pool_size": len(pool), "sampled": len(posts),
              "sampling": sampling, "warnings": warns, "categories": cats, "source_input": str(Path(args.input).resolve()),
              "model_input_samples": lines,
              "sampled_posts_evidence": [{"excel_row": r.get('_source_excel_row'),
                  "id": r.get('id', r.get('ID')), "url": r.get('链接', r.get('URL', r.get('原文链接'))),
                  "title": str(r.get('标题', '') or ''), "content": str(r.get('内容', '') or '')} for r in posts]}
    for w in warns:
        print(f"⚠️ {w}", file=sys.stderr)
    print(f"✅ 发现 {len(cats)} 个参与方式类别（基于 {len(posts)}/{len(pool)} 条样本）", file=sys.stderr)
    for c in cats:
        print(f"  【{c.get('name', '?')}】{c.get('definition', '')}", file=sys.stderr)
    print("⚠️  需独立 Validator 对照来源帖子审核通过后，才可注入 annotation_prompt 并执行轮次二", file=sys.stderr)
    write_result(args.output, result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
