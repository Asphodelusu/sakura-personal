"""plugins/mmd_renderer/config.py — MMD 角色渲染配置解析。

把角色清单里的 ``renderer`` 段（type/model/motions/expressions/lip_sync/
event_motions 等）解析为结构化配置，并把模型/动作的相对路径解析为
**相对角色目录** 的本地 file URL，供 Python→JS 的 loadCharacter 消息使用。

路径处理要点（MMD 资源常见中文/日文/空格路径）：
- 相对路径相对角色包目录解析，绝对路径原样保留；
- 用 ``QUrl.fromLocalFile`` 生成跨平台 file URL（自动处理反斜杠、空格、
  非 ASCII 字符转义），不手工拼接 URL；
- 资源文件缺失不抛异常（框架阶段允许模型尚未就位），记录到
  :attr:`missing_paths` 并写日志，由上层决定是否提示。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl

from app.core.debug_log import debug_log


def _resolve_package_path(package_dir: Path, path_text: str) -> Path:
    """解析角色资源路径：相对路径相对角色目录，绝对路径原样；统一规范为绝对路径。

    去除首尾引号/空白，兼容 Windows 反斜杠与中文/日文路径。最后 resolve() 规范为
    绝对路径——否则当 package_dir 为相对路径时，QUrl.fromLocalFile 会生成相对
    file URL（``file:xxx`` 而非 ``file:///``），网页 loader 无法加载。
    """
    cleaned = path_text.strip().strip('"').strip("'")
    path = Path(cleaned)
    if not path.is_absolute():
        path = package_dir / path
    return path.resolve()


def _to_file_url(path: Path) -> str:
    """生成跨平台本地 file URL（完全百分号编码）。

    QUrl.toString() 默认 PrettyDecoded 会保留裸空格与非 ASCII 字符，作为本地
    资源 URL 交给网页 fetch/loader 可能加载失败；改用 toEncoded() 得到完全
    编码的 ASCII URL（空格→%20、中日文→%XX），确保跨平台可靠加载。
    """
    return bytes(QUrl.fromLocalFile(str(path)).toEncoded()).decode("ascii")


@dataclass(frozen=True)
class MMDRendererConfig:
    """解析后的 MMD 渲染配置。"""

    renderer_type: str = "mmd"
    fallback: str = "default"
    scale: float = 1.0
    hide_default_portrait: bool = False
    model_path: Path | None = None
    model_url: str | None = None
    # 动作名 -> 本地 file URL / 解析后的 Path
    motions: dict[str, str] = field(default_factory=dict)
    motion_paths: dict[str, Path] = field(default_factory=dict)
    # 表情名 -> {morph 名: 权重}
    expressions: dict[str, dict[str, float]] = field(default_factory=dict)
    # 口型配置：{"morph": str, "strength": float}
    lip_sync: dict[str, Any] = field(default_factory=dict)
    # 事件名 -> 动作名
    event_motions: dict[str, str] = field(default_factory=dict)
    # 解析时不存在的资源文件（记录但不阻断）
    missing_paths: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, cfg: dict[str, Any] | None, package_dir: Path) -> "MMDRendererConfig":
        cfg = cfg if isinstance(cfg, dict) else {}
        missing: list[str] = []

        def resolve_file(rel: Any, label: str) -> tuple[Path | None, str | None]:
            if not isinstance(rel, str) or not rel.strip():
                return None, None
            path = _resolve_package_path(package_dir, rel)
            if not path.exists():
                missing.append(f"{label}: {path}")
                debug_log("MMDRenderer", "资源文件不存在（框架阶段忽略）", {"label": label, "path": str(path)})
            return path, _to_file_url(path)

        model_path, model_url = resolve_file(cfg.get("model"), "model")

        motions: dict[str, str] = {}
        motion_paths: dict[str, Path] = {}
        raw_motions = cfg.get("motions")
        if isinstance(raw_motions, dict):
            for name, rel in raw_motions.items():
                if not isinstance(name, str):
                    continue
                path, url = resolve_file(rel, f"motion[{name}]")
                if path is not None and url is not None:
                    motion_paths[name] = path
                    motions[name] = url

        expressions: dict[str, dict[str, float]] = {}
        raw_expr = cfg.get("expressions")
        if isinstance(raw_expr, dict):
            for name, morphs in raw_expr.items():
                if not isinstance(name, str):
                    continue
                expressions[name] = _coerce_morphs(morphs)

        lip_sync: dict[str, Any] = {}
        raw_lip = cfg.get("lip_sync")
        if isinstance(raw_lip, dict):
            morph = raw_lip.get("morph")
            if isinstance(morph, str) and morph.strip():
                lip_sync["morph"] = morph.strip()
            lip_sync["strength"] = _coerce_float(raw_lip.get("strength"), 0.8)

        event_motions: dict[str, str] = {}
        raw_event = cfg.get("event_motions")
        if isinstance(raw_event, dict):
            for event_name, motion_name in raw_event.items():
                if isinstance(event_name, str) and isinstance(motion_name, str):
                    event_motions[event_name.strip()] = motion_name.strip()

        return cls(
            renderer_type=str(cfg.get("type") or "mmd").strip().lower(),
            fallback=str(cfg.get("fallback") or "default").strip().lower(),
            scale=_coerce_float(cfg.get("scale"), 1.0),
            hide_default_portrait=bool(cfg.get("hide_default_portrait", False)),
            model_path=model_path,
            model_url=model_url,
            motions=motions,
            motion_paths=motion_paths,
            expressions=expressions,
            lip_sync=lip_sync,
            event_motions=event_motions,
            missing_paths=missing,
        )

    def to_payload(self) -> dict[str, Any]:
        """转换为 Python→JS 的 loadCharacter 消息 payload（路径均为 file URL）。"""
        return {
            "type": self.renderer_type,
            "scale": self.scale,
            "model": self.model_url,
            "motions": dict(self.motions),
            "expressions": {name: dict(morphs) for name, morphs in self.expressions.items()},
            "lipSync": dict(self.lip_sync),
            "eventMotions": dict(self.event_motions),
        }


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_morphs(morphs: Any) -> dict[str, float]:
    if not isinstance(morphs, dict):
        return {}
    result: dict[str, float] = {}
    for morph_name, weight in morphs.items():
        if isinstance(morph_name, str):
            result[morph_name] = _coerce_float(weight, 0.0)
    return result
