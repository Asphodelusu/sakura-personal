"""Recall query planning: one intent plus key entities instead of a noisy concatenation.

A fast model may rewrite the query; any failure falls back to the heuristic.
"""
import json
import re

if __package__:
    from .personal_entities import extract_entities, find_known_entity_aliases, is_known_entity_alias
else:
    from personal_entities import extract_entities, find_known_entity_aliases, is_known_entity_alias

MAX_QUERY_CHARS = 4000
MAX_ENTITIES = 8
REWRITE_MAX_TOKENS = 120
_REFERENTIAL = re.compile(r"(他|她|它|这事|那事|那个|这个|上次|之前|刚才|还记得|记不记得|怎么样了|后来呢)")
_TITLE = re.compile(r"《([^》]{1,40})》")
_PROPER_NAME = re.compile(r"[\u30a0-\u30ffA-Za-z]")
_STOPWORDS = frozenset({
    "对方", "最近", "有没有", "怎么样", "什么", "一个", "我们", "今天", "明天", "周末",
    "早上", "晚上", "后来", "清楚", "提到", "再提", "是不是", "继续", "还是",
})
_REWRITE_SYSTEM_PROMPT = (
    "你是记忆检索 query 改写器。根据当前用户输入，产出一句短检索意图（中文为主，可保留作品原名），"
    "并列出关键实体（人名/作品名/专有名词）。\n"
    "规则：\n"
    "1. query 只表达本轮要回忆的主题，不要混入无关闲聊或屏幕摘要。\n"
    "2. 若当前句有指代（他/那个/上次），可借助 recent_user_messages 补全指代对象。\n"
    "3. 不要编造输入里没有的事实。\n"
    '只输出 JSON：{"query":"...","entities":["..."]}'
)


def _recent_user(request):
    return [message.content.strip() for message in request.recent_messages
            if message.role == "user" and message.content.strip()]


def baseline_query(request):
    parts = []
    if request.current_input.strip():
        parts.append(request.current_input.strip())
    parts.extend(_recent_user(request)[-2:])
    parts.extend(summary.strip() for summary in request.visual_summaries if summary.strip())
    return "\n".join(dict.fromkeys(parts)).strip()[:MAX_QUERY_CHARS].rstrip()


def _query_entities(text):
    ordered = []
    for title in _TITLE.findall(text or ""):
        name = title.strip()
        if name and name not in ordered:
            ordered.append(name)
    for name in sorted(extract_entities(text), key=len, reverse=True):
        clean = name.strip()
        if (not clean or clean in _STOPWORDS
                or not (_PROPER_NAME.search(clean) or len(clean) >= 3 or is_known_entity_alias(clean))):
            continue
        if clean not in ordered:
            ordered.append(clean)
        if len(ordered) >= MAX_ENTITIES:
            break
    return tuple(ordered[:MAX_ENTITIES])


def heuristic_query(request):
    current = request.current_input.strip()
    recent = [text for text in _recent_user(request) if text != current]
    parts = [current] if current else recent[-1:]
    if current and recent and (len(current) <= 10 or _REFERENTIAL.search(current)):
        parts.append(recent[-1])
    source = "\n".join(parts)
    entities = _query_entities(source)
    known = tuple(name for name in sorted(find_known_entity_aliases(source), key=len, reverse=True)
                  if name not in entities)
    entities = (entities + known)[:MAX_ENTITIES]
    if entities:
        parts.append("关键实体：" + "、".join(entities))
    return "\n".join(dict.fromkeys(part for part in parts if part)).strip()[:MAX_QUERY_CHARS].rstrip()


def model_query(request, client):
    current = request.current_input.strip()
    if not current or client is None:
        return ""
    payload = {
        "current_input": current,
        "recent_user_messages": _recent_user(request)[-2:],
        "visual_summaries": [summary.strip() for summary in request.visual_summaries if summary.strip()][:2],
    }
    try:
        raw = client.complete_raw(
            _REWRITE_SYSTEM_PROMPT,
            [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            temperature=0.2,
            max_tokens=REWRITE_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
    except Exception:
        return ""
    return parse_rewrite(raw)


def parse_rewrite(raw):
    text = str(raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return ""
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    query = str(data.get("query") or "").strip()
    if not query:
        return ""
    entities = []
    for item in data.get("entities") if isinstance(data.get("entities"), list) else []:
        name = str(item or "").strip()
        if name and name not in entities:
            entities.append(name)
    if entities:
        query = f"{query}\n关键实体：" + "、".join(entities[:MAX_ENTITIES])
    return query[:MAX_QUERY_CHARS].rstrip()


def plan_query(request, client=None):
    planned = model_query(request, client)
    if planned:
        return planned
    if not request.current_input.strip() and not _recent_user(request):
        return baseline_query(request)
    return heuristic_query(request)
