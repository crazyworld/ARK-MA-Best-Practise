"""Bounded Boolean syntax validation, never eval or provider-semantic guessing."""
from errors import SkillError

MAX_CHARS = 8192
MAX_DEPTH = 32
STAT_GROUP_LIMIT = 50


def parse(text, *, allow_empty=False, limit=STAT_GROUP_LIMIT):
    if not isinstance(text, str): raise SkillError('EXPRESSION_STRING_REQUIRED')
    if text == '' and allow_empty: return dict(ast=None, groups=0, escaped=False)
    if not text.strip(): raise SkillError('EXPRESSION_EMPTY')
    if len(text)>MAX_CHARS: raise SkillError('EXPRESSION_TOO_LONG')
    tokens=[];word=[];escaped=False;i=0
    def flush():
        if word:
            value=''.join(word).strip();word.clear()
            if value:tokens.append(('term',value))
    while i<len(text):
        ch=text[i]
        if ord(ch)<32: raise SkillError('EXPRESSION_CONTROL_CHARACTER')
        if ch=='\\':
            i+=1
            if i==len(text) or text[i] not in '+|()\\':raise SkillError('EXPRESSION_INVALID_ESCAPE')
            word.append(text[i]);escaped=True
        elif ch in '+|()':
            flush();tokens.append((ch,ch))
        else:word.append(ch)
        i+=1
    flush();pos=0
    def atom(depth):
        nonlocal pos
        if depth>MAX_DEPTH:raise SkillError('EXPRESSION_TOO_DEEP')
        if pos==len(tokens):raise SkillError('EXPRESSION_OPERAND_REQUIRED')
        kind,value=tokens[pos];pos+=1
        if kind=='term':return ('term',value),1
        if kind=='(':
            node,count=group(depth+1)
            if pos==len(tokens) or tokens[pos][0]!=')':raise SkillError('EXPRESSION_UNBALANCED_PARENTHESES')
            pos+=1;return node,count
        raise SkillError('EXPRESSION_OPERAND_REQUIRED')
    def group(depth):
        nonlocal pos
        first,count=atom(depth);nodes=[first];operator=None
        while pos<len(tokens) and tokens[pos][0]!=')':
            op=tokens[pos][0]
            if op not in ('+','|'):raise SkillError('EXPRESSION_OPERATOR_REQUIRED')
            if operator and operator!=op:raise SkillError('EXPRESSION_MIXED_OPERATORS_REQUIRE_PARENTHESES')
            operator=op;pos+=1;node,n=atom(depth);nodes.append(node)
            # Saturated arithmetic avoids materializing exponential DNF expansion.
            count=min(limit+1,count+n if op=='|' else count*n)
        return (operator,tuple(nodes)) if operator else first,count
    ast,count=group(0)
    if pos!=len(tokens):raise SkillError('EXPRESSION_UNBALANCED_PARENTHESES')
    if count>limit:raise SkillError('EXPRESSION_GROUP_LIMIT_EXCEEDED')
    return dict(ast=ast,groups=count,escaped=escaped)


def validate_for_provider(text, *, allow_empty=False, limit=STAT_GROUP_LIMIT):
    result=parse(text,allow_empty=allow_empty,limit=limit)
    # Create and statistics documentation do not yet agree on escape wire layers.
    # Syntax is understood; do not send a guessed representation to paid endpoints.
    if result['escaped']:raise SkillError('EXPRESSION_ESCAPE_MAPPING_UNVERIFIED',
        next_action='表达式含字面转义符；两端转义传输契约未确认，停止收费调用并交维护方核对。')
    return result
