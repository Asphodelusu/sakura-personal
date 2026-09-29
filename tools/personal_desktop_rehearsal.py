"""Isolated desktop trial; personal work copies and real chat are explicit."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread


MARKER = ".personal-desktop-rehearsal.json"


@contextmanager
def temporary_live_api(source: Path, target: Path, *, curation_selection=None):
    import yaml
    if source.resolve() == target.resolve():
        raise ValueError("Live API source must be outside the rehearsal configuration")
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    profiles, slots = {}, {}
    selections = [("chat", raw.get("model_slots", {}).get("chat", {}))]
    for purpose in ("inner_thought", "chat_fast"):
        if purpose not in raw.get("model_slots", {}):
            continue
        slot = raw["model_slots"][purpose]
        if not isinstance(slot, dict):
            raise ValueError(f"The supplied API configuration has an invalid {purpose} selection")
        profile_id, model = slot.get("profile_id", ""), slot.get("model", "")
        if (not isinstance(profile_id, str) or not isinstance(model, str)
                or bool(profile_id.strip()) != bool(model.strip())):
            raise ValueError(f"The supplied API configuration has an invalid {purpose} selection")
        if profile_id.strip():
            selections.append((purpose, {"profile_id": profile_id.strip(), "model": model.strip()}))
    if curation_selection:
        selections.append(("memory_curation", curation_selection))
    for purpose, slot in selections:
        profile = next((p for p in raw.get("api_profiles", [])
                        if p.get("id") == slot.get("profile_id")), None)
        if (not profile or not slot.get("model") or not profile.get("base_url") or not profile.get("api_key")):
            raise ValueError(f"The supplied API configuration has no usable {purpose} selection")
        selected = profiles.setdefault(profile["id"], {
            **{key: profile[key] for key in ("id", "base_url", "api_key")},
            "alias": profile.get("alias") or profile["id"], "models": []})
        model = {"name": slot["model"]}
        if model not in selected["models"]:
            selected["models"].append(model)
        slots[purpose] = {"profile_id": profile["id"], "model": slot["model"]}
    value = {"api_profiles": list(profiles.values()), "model_slots": slots, "config_version": 1}
    before = target.read_bytes() if target.exists() else None
    try:
        target.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
        yield
    finally:
        if before is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(before)


def prepare(repository: Path, target: Path, snapshot: Path) -> None:
    # Refuse before any imports/model loads or writes to an existing destination.
    if target.exists():
        raise FileExistsError(target)
    dependencies = repository / "plugins/personal-dependencies-py312"
    for required in (repository / "runtime/python.exe", snapshot / "config.json", dependencies):
        if not required.exists():
            raise FileNotFoundError(required)
    from app.legacy_import.personal_copy import prepare_personal_copy
    from app.storage.paths import StoragePaths
    from tests.personal_memory_fixture import create_synthetic_memory, install_personal_plugin

    target.mkdir(parents=True)
    distribution, user = target / "distribution", target / "user"
    seed = target / "synthetic-source"
    print("Preparing synthetic BGE-M3 memory...", flush=True)
    create_synthetic_memory(seed, snapshot)
    prepare_personal_copy(seed, user, source_is_quiescent=lambda: True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    print("Preparing isolated runtime and plugin distribution...", flush=True)
    for name in ("app", "runtime"):
        shutil.copytree(repository / name, distribution / name, ignore=ignore)
    shutil.copytree(repository / "desktop/src-tauri/runtime-layouts",
                    distribution / "desktop/src-tauri/runtime-layouts")
    shutil.copy2(repository / "VERSION", distribution / "VERSION")
    install_personal_plugin(repository, distribution, dependencies)
    shutil.copytree(repository / "plugins/builtin/sakura_portrait",
                    distribution / "plugins/builtin/sakura_portrait", ignore=ignore)
    fixture = repository / "tests/fixtures/runtime_v2/wp_3_01/ready"
    shutil.copytree(fixture, user, dirs_exist_ok=True)
    character = user / "characters/sakura/character.json"
    value = json.loads(character.read_text(encoding="utf-8"))
    value["display_name"] = "Sakura 隔离演练"
    value["initial_message"] = "这是合成数据演练。可以问：蓝色笔记本在哪里？"
    value["portrait"] = {"default": "portraits/preview.png", "expressions": {"neutral": "portraits/preview.png"}}
    character.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    shutil.copy2(repository / "desktop/src-tauri/icons/icon.png", user / "characters/sakura/portraits/preview.png")
    (user / "characters/sakura/portraits/neutral.txt").unlink()
    config = StoragePaths(user).plugin_data_for("sakura.memory.mem0") / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"personalSnapshot": str(snapshot)}), encoding="utf-8")
    (user / "config/ui.json").write_text(json.dumps({
        "schema_version": 1, "domain": "ui",
        "settings": {"first_run_guide_completed": True, "telemetry": {"enabled": False}},
    }), encoding="utf-8")
    # Presence means all preparation finished; incomplete roots are not runnable.
    (target / MARKER).write_text(json.dumps({"schema": 1, "kind": "synthetic-only"}), encoding="utf-8")


def start_provider(report: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4 * 1024 * 1024:
                self.send_error(413)
                return
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self.send_error(400)
                return
            messages = json.dumps(body.get("messages", []), ensure_ascii=False)
            recalled = "CORE_RECALL_312" in messages
            foreign = "FOREIGN_SCOPE_312" in messages
            report.write_text(json.dumps({
                "memory_in_request": recalled, "foreign_memory_in_request": foreign,
                "requests": getattr(self.server, "request_count", 0) + 1,
            }), encoding="utf-8")
            self.server.request_count = getattr(self.server, "request_count", 0) + 1
            if foreign:
                self.send_error(500, "Synthetic scope isolation failed")
                return
            answer = ("已收到本轮召回的合成记忆：蓝色笔记本在厨房抽屉里。"
                      if recalled else "本轮尚未收到合成记忆；模型可能仍在加载。稍后再问蓝色笔记本在哪里。")
            japanese = ("青いノートは台所の引き出しにあります。" if recalled
                        else "まだ記憶を読み込めていません。少し待ってから、もう一度聞いてください。")
            content = json.dumps({"segments": [{"ja": japanese, "zh": answer,
                                                "tone": "neutral", "portrait": "neutral"}]}, ensure_ascii=False)
            payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]},
                                 ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, name="personal-rehearsal-api")
    thread.start()
    return server, thread


def stop_provider(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(5)
    if thread.is_alive():
        raise RuntimeError("Local rehearsal provider did not stop")


def trial_layout(target: Path):
    marker = json.loads((target / MARKER).read_text(encoding="utf-8"))
    if marker == {"schema": 1, "kind": "synthetic-only"}:
        return target / "user", target / "distribution", "sakura", None
    if marker.get("schema") != 1 or marker.get("kind") != "personal-copy":
        raise ValueError("Not a prepared rehearsal")
    import yaml
    user, distribution, cache = (Path(marker[key]).resolve(strict=True)
                                 for key in ("user_root", "distribution", "bm25_cache"))
    copy = json.loads((user / ".sakura-personal-copy.json").read_text(encoding="utf-8"))
    if copy.get("state") != "complete" or copy.get("role") != "work":
        raise ValueError("Personal trial requires a complete work copy, never a baseline")
    character = yaml.safe_load((user / "config/characters.yaml").read_text(encoding="utf-8"))
    character_id = character.get("current_character_id")
    if not isinstance(character_id, str) or not character_id.strip():
        raise ValueError("Personal trial requires the original character ID")
    return user, distribution, character_id, cache


def run(repository: Path, target: Path, *, live_api: Path | None = None,
        tts_runtime: Path | None = None, write_rehearsal=False) -> int:
    user, distribution, character_id, bm25_cache = trial_layout(target)
    import yaml
    manifest_path = distribution / "plugins/builtin/sakura_mem0/plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    writable_entry = manifest.get("entry") == "plugin:PersonalWriteRehearsalPlugin"
    if writable_entry != write_rehearsal:
        raise ValueError("Writable plugin entry and --write-rehearsal must be enabled together")
    curation_selection = None
    if write_rehearsal:
        from plugins.builtin.sakura_mem0.personal_records import _require_write_rehearsal
        _require_write_rehearsal(user / "data/memory")
        if not writable_entry or live_api is None:
            raise ValueError("Writable trial requires its explicit plugin entry and live model slots")
        plugin_config = json.loads((user / "data/plugins/sakura.memory.mem0/config.json").read_text(encoding="utf-8"))
        profile_id = plugin_config.get("curationProfileId", "")
        model = plugin_config.get("curationModel", "")
        if not isinstance(profile_id, str) or not isinstance(model, str) or bool(profile_id) != bool(model):
            raise ValueError("Writable trial has an invalid private curation model selection")
        curation_selection = ({"profile_id": profile_id, "model": model}
                              if profile_id else {})
    if bm25_cache is not None and (live_api is None or tts_runtime is None):
        raise ValueError("Personal trial requires explicit live API and GPT-SoVITS paths")
    from app.core.instance import SingleInstanceGuard, InstanceAcquireStatus
    guard = SingleInstanceGuard()
    try:
        if guard.acquire() is not InstanceAcquireStatus.ACQUIRED:
            raise RuntimeError("请先从菜单退出正在运行的 Sakura；演练不会关闭已有实例。")
    finally:
        guard.release()
    server, thread = (start_provider(target / "provider-report.json")
                      if live_api is None else (None, None))
    resources = ExitStack()
    process = None
    try:
        if tts_runtime is not None:
            from tools.personal_rehearsal_voice import managed_voice
            resources.enter_context(managed_voice(tts_runtime, target,
                                                  user_root=user, character_id=character_id))
        # Default launches stay local; live credentials are restored on every exit path.
        if live_api is not None:
            resources.enter_context(temporary_live_api(live_api, user / "config/api.yaml",
                                                       curation_selection=curation_selection))
        else:
            (user / "config/api.yaml").write_text(
                "api_profiles:\n  - id: rehearsal\n    alias: Local synthetic provider\n"
                f"    base_url: http://127.0.0.1:{server.server_port}/v1\n"
                "    api_key: LOCAL_SYNTHETIC_ONLY\n    models:\n      - name: rehearsal\n"
                "model_slots:\n  chat:\n    profile_id: rehearsal\n    model: rehearsal\nconfig_version: 1\n",
                encoding="utf-8")
        environment = os.environ.copy()
        environment.pop("SAKURA_WP_4_01_MANUAL_ROOT", None)
        environment.update(SAKURA_RUNTIME_USER_ROOT=str(user), HF_HUB_OFFLINE="1",
                           TRANSFORMERS_OFFLINE="1", MEM0_TELEMETRY="False",
                           WEBVIEW2_USER_DATA_FOLDER=str(target / "webview"))
        if bm25_cache is not None:
            environment["FASTEMBED_CACHE_PATH"] = str(bm25_cache)
        print(f"Isolated trial user root: {user}", flush=True)
        print("请等待记忆模型加载，再开始验收；从 Sakura 菜单退出，以恢复配置并关闭演练语音服务。", flush=True)
        process = subprocess.Popen([str(repository / "desktop/src-tauri/target/debug/sakura.exe")],
                                   cwd=distribution, env=environment)
        result = process.wait()
        print(f"Desktop exited: {result}", flush=True)
        return result
    finally:
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        finally:
            resources.close()
            if server is not None:
                stop_provider(server, thread)


def main():
    repository = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repository), str(repository / "app/plugin_sdk"),
                   str(repository / "plugins/personal-dependencies-py312")]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="Explicit prepared isolated trial directory")
    parser.add_argument("--prepare", type=Path, metavar="LOCAL_BGE_SNAPSHOT")
    parser.add_argument("--live-api", type=Path, metavar="API_CONFIG",
                        help="Temporarily use only this file's selected real chat profile")
    parser.add_argument("--tts-runtime", type=Path, metavar="GPT_SOVITS_DIRECTORY",
                        help="Start and own an isolated voice service using prepared voice resources")
    parser.add_argument("--write-rehearsal", action="store_true", help="Use both model slots on an admitted writable copy")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Use the candidate Windows runtime/python.exe (3.12)")
    target = args.target.resolve(strict=True) if args.target else repository / "temp/personal-desktop-rehearsal"
    if args.prepare:
        prepare(repository, target, args.prepare.resolve(strict=True))
        print(f"Prepared: {target}")
        return 0
    return run(repository, target, live_api=args.live_api, tts_runtime=args.tts_runtime,
               write_rehearsal=args.write_rehearsal)


if __name__ == "__main__":
    raise SystemExit(main())
