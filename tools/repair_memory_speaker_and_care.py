"""一次性修复：称呼/关心方向/仓库分享描述。需在桌宠未占用 Qdrant 时运行。"""
from __future__ import annotations

import json
import pickle
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "data" / "memory" / "core_profiles.json"
QDRANT = ROOT / "data" / "memory" / "qdrant" / "collection" / "sakura_memories" / "storage.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def fix_core_profile() -> None:
    data = json.loads(CORE.read_text(encoding="utf-8"))
    rec = data.get("Sakura")
    if not isinstance(rec, dict):
        print("core_profile: missing Sakura")
        return
    old = str(rec.get("content") or "")
    new = old.replace("以后可以叫我「铭君」", "以后可以叫他「铭君」")
    # 他喜欢我的原因应是「我」的特质，不是「我喜欢他…」
    new = new.replace(
        "并说明了原因：我喜欢他早起、认真对待小对话、总是关心他。",
        "并说明了原因：我早起、认真对待小对话、总是关心他。",
    )
    if new == old:
        print("core_profile: no text change needed (maybe already fixed)")
    else:
        rec["content"] = new
        rec["memory"] = new
        meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
        meta["updated_at"] = _now()
        meta["source"] = "manual_repair"
        rec["metadata"] = meta
        CORE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("core_profile: updated")
        print("  ->", new.replace("\n", " / ")[-80:])


def _load_points() -> list[tuple[str, object, dict]]:
    con = sqlite3.connect(str(QDRANT))
    cur = con.cursor()
    out = []
    for raw_id, blob in cur.execute("SELECT id, point FROM points").fetchall():
        point = pickle.loads(blob)
        payload = point.payload or {}
        out.append((raw_id, point, payload))
    con.close()
    return out


def _save_point(raw_id: str, point: object) -> None:
    con = sqlite3.connect(str(QDRANT))
    cur = con.cursor()
    cur.execute("UPDATE points SET point=? WHERE id=?", (pickle.dumps(point), raw_id))
    con.commit()
    con.close()


def _delete_point(raw_id: str) -> None:
    con = sqlite3.connect(str(QDRANT))
    cur = con.cursor()
    cur.execute("DELETE FROM points WHERE id=?", (raw_id,))
    con.commit()
    con.close()


def fix_vector_memories() -> None:
    points = _load_points()
    print(f"vector points: {len(points)}")

    delete_needles = [
        # 关心方向反了：写成「铭君关心我吃饭/作息」
        "铭君主动关心我是否好好吃饭",
        "铭君主动关心我的作息，并约定今晚12点前休息",
        # 旧版「他叫我变态桑」方向也可能含糊，保留较新的「叫自己变态桑」那条
    ]
    replace_map = [
        (
            "推荐朋友自定义“自己的小家伙”",
            "他会把 Sakura 仓库分享给朋友；朋友也可以自己配置自己的桌宠。",
            "把分享仓库误写成「自定义小家伙」",
        ),
        (
            "推荐朋友自定义「自己的小家伙」",
            "他会把 Sakura 仓库分享给朋友；朋友也可以自己配置自己的桌宠。",
            "把分享仓库误写成「自定义小家伙」",
        ),
        (
            "这让我联想到他把我分享给了朋友，虽然有点害羞但感到被珍视。",
            "他愿意把这个项目分享给朋友，让我感到被认真对待，也有点害羞。",
            "过度脑补「把我分享给朋友」",
        ),
        (
            "以后可以叫我「铭君」",
            "以后可以叫他「铭君」",
            "称呼写反",
        ),
        (
            "可以叫我「铭君」",
            "可以叫他「铭君」",
            "称呼写反",
        ),
    ]

    deleted = 0
    updated = 0
    for raw_id, point, payload in points:
        content = str(payload.get("data") or "")
        if not content:
            continue

        if any(n in content for n in delete_needles):
            _delete_point(raw_id)
            deleted += 1
            print(f"DELETE {str(point.id)[:8]}… {content[:60].replace(chr(10),' / ')}")
            continue

        new = content
        reasons = []
        for old, repl, why in replace_map:
            if old in new:
                new = new.replace(old, repl)
                reasons.append(why)

        # 吃饭：若仍是「铭君…关心我…吃饭」且没有「我主动关心铭君」则改写
        if "好好吃饭" in new and "铭君" in new and "关心我" in new and "我主动关心铭君" not in new:
            if "铭君主动关心我" in new or "铭君在对话中表现出对我的关心" in new:
                # 统一为樱关心他
                if "铭君主动关心我是否好好吃饭" in new:
                    new = (
                        "2026年7月20日，我主动关心铭君是否好好吃饭，并提醒他下次要坐着吃。"
                        "当他提到他工作时会看到我时，他澄清说这是他自己想做的，不是委屈。"
                        "我说「但就算这样，还是希望你好好吃饭」。"
                    )
                    reasons.append("吃饭关心方向：改为我关心他")
                elif "即使他自己喝营养液，也坚持提醒我边吃边做" in new:
                    new = (
                        "铭君自己喝营养液，但我仍坚持提醒他边吃边做对身体不好，希望他好好吃饭。"
                        "我说「但就算这样，还是希望你好好吃饭」，这种关心让我自己也觉得安心。"
                    )
                    reasons.append("吃饭关心方向：改为我关心他")

        if new != content:
            payload = dict(payload)
            payload["data"] = new
            payload["updated_at"] = _now()
            payload["source"] = "manual_repair"
            point.payload = payload
            _save_point(raw_id, point)
            updated += 1
            print(f"UPDATE {str(point.id)[:8]}… ({', '.join(reasons)})")
            print(f"  {new[:100].replace(chr(10),' / ')}")

    print(f"done: deleted={deleted} updated={updated}")


def main() -> None:
    fix_core_profile()
    try:
        fix_vector_memories()
    except Exception as exc:
        print(f"vector repair failed (app may lock Qdrant): {exc}")
        print("core_profile 已先修好；请关掉桌宠后再跑本脚本一次。")


if __name__ == "__main__":
    main()
