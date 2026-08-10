"""从 Sakura.db 抽取日→中翻译基准数据集（只读，不修改历史库）。

输出: artifacts/agent-benchmarks/translation-decoupling/dataset.jsonl
（每条含 ja / zh_reference / tone / labels；本地私有数据，不入库）
标注为启发式辅助标签，供人工盲评参考，非精确判断。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB = REPO_ROOT / "data/chat_history/Sakura.db"
OUT_DIR = REPO_ROOT / "artifacts/agent-benchmarks/translation-decoupling"

# ---- 特殊标注启发式规则 ----
_EXPLICIT_SUBJECT = ("私は", "私が", "僕は", "俺は", "あたしは", "あなた", "あんた", "お前", "君は", "きみは")
_DEFLECTION_RE = re.compile(r"(別に|べつに|じゃない|じゃね|ではない|なんか|わけない|ふん|〜なくてもいい|〜しなくていい)")
_JEALOUSY_RE = re.compile(r"(ヤキモチ|妬いて|嫉妬|比べて|どっちが|〜が好きなの|浮気|うわき)")
_REQUEST_RE = re.compile(r"(して|してよ|してね|してほしい|してくれる|お願い|ください|ちょうだい|〜て)")
_CALL_RE = re.compile(r"(さん|くん|ちゃん|先輩|君|あんた|お前|あなた|姉さん|兄さん|ねえ)")
_INTIMACY_RE = re.compile(r"(ちゅ|キス|きす|抱きしめ|抱いて|愛してる|好き|ぎゅ|もっと|ねえ、|ねぇ|キスして)")
_SYSTEM_MARK_RE = re.compile(r"(【|システム|システムエラー|エラー|エラーが|失敗|通知|提醒|抱歉|错误)")


def annotate(ja: str, zh: str, tone: str | None, debug: str | None) -> list[str]:
    labels: list[str] = []
    # 省略主语：句内无显式第一/第二人称主语（日语普遍省略，标出供翻译时判断补主语）
    if not any(w in ja for w in _EXPLICIT_SUBJECT):
        labels.append("省略主语")
    if _DEFLECTION_RE.search(ja):
        labels.append("傲娇否定")
    if (tone == "吃醋") or _JEALOUSY_RE.search(ja):
        labels.append("吃醋")
    if (tone == "请求") or _REQUEST_RE.search(ja):
        labels.append("请求")
    if _CALL_RE.search(ja):
        labels.append("称谓")
    if (tone in ("亲密", "H")) or _INTIMACY_RE.search(ja):
        labels.append("亲密语气")
    if debug and "unknown" in str(debug):
        labels.append("疑似降级(debug)")
    if _SYSTEM_MARK_RE.search(ja):
        labels.append("疑似系统文案")
    return labels


def main(limit: int = 200) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        """
        SELECT id, created_at, content, translation, tone, channel, debug
        FROM chat_history
        WHERE role='assistant'
          AND content IS NOT NULL AND trim(content) != ''
          AND translation IS NOT NULL AND trim(translation) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    items = []
    for rid, ts, ja, zh, tone, channel, debug in rows:
        items.append({
            "id": rid,
            "created_at": ts,
            "ja": ja,
            "zh_reference": zh,
            "tone": tone or "",
            "channel": channel or "",
            "labels": annotate(ja, zh, tone, debug),
            "ja_chars": len(ja),
            "zh_chars": len(zh),
        })

    tones = Counter(i["tone"] for i in items)
    print("tone 分布:", dict(tones))
    labels = Counter(l for i in items for l in i["labels"])
    print("标注分布:", dict(labels))

    out_path = OUT_DIR / "dataset.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for i in items:
            f.write(json.dumps(i, ensure_ascii=False) + "\n")
    print(f"已写入 {out_path}（{len(items)} 条）")


if __name__ == "__main__":
    main(limit=int(sys.argv[1]) if len(sys.argv) > 1 else 200)
