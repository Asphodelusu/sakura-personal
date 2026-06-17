// sakura_mmd.js — SakuraMMD 控制器骨架。
//
// 接收 Python 经 runJavaScript 注入的统一消息（{type, payload}），分发到各
// handler。本期各 handler 仅记录日志，验证 Python→JS 消息链路；真正的渲染
// 在后续接入 three.js / MMDLoader 后实现。
//
// 注：使用经典脚本而非 ES module（见 index.html 说明），类定义后挂到全局。
(function () {
  "use strict";

  class SakuraMMD {
    constructor(options) {
      options = options || {};
      this.canvas = options.canvas || null;
      this.characterConfig = null;
      this.ready = false;
      this.lipSyncValue = 0;
    }

    initialize() {
      console.log("[SakuraMMD] initialize");
      this.ready = true;

      // three.js 运行时占位初始化（当前不加载实际模型）。
      if (window.SakuraMMDRuntime && typeof window.SakuraMMDRuntime.init === "function") {
        try {
          window.SakuraMMDRuntime.init(this.canvas);
        } catch (err) {
          console.warn("[SakuraMMD] runtime init failed", err);
        }
      }
    }

    dispatch(message) {
      console.log("[SakuraMMD] dispatch", message);

      if (!message || !message.type) {
        console.warn("[SakuraMMD] invalid message", message);
        return;
      }

      switch (message.type) {
        case "loadCharacter":
          return this.loadCharacter(message.payload);
        case "playMotion":
          return this.playMotion(message.payload);
        case "stopMotion":
          return this.stopMotion(message.payload);
        case "setExpression":
          return this.setExpression(message.payload);
        case "setLipSync":
          return this.setLipSync(message.payload);
        case "lookAt":
          return this.lookAt(message.payload);
        case "setScale":
          return this.setScale(message.payload);
        case "event":
          return this.handleEvent(message.payload);
        case "ping":
          return console.log("[SakuraMMD] pong");
        default:
          console.warn("[SakuraMMD] unknown message type", message.type);
      }
    }

    loadCharacter(payload) {
      this.characterConfig = payload;
      // 显式拼出关键字段：console.log(obj) 经 Qt 转发会被简化为 [object Object]。
      var model = (payload && payload.model) || "(none)";
      var scale = (payload && typeof payload.scale === "number") ? payload.scale : 1.0;
      console.log("[SakuraMMD] loadCharacter model=" + model + " scale=" + scale);

      if (model && window.SakuraMMDRuntime && window.SakuraMMDRuntime.loadModel) {
        window.SakuraMMDRuntime.loadModel(model, scale);
      } else {
        console.warn("[SakuraMMD] runtime.loadModel unavailable or model missing");
      }
    }

    playMotion(payload) {
      console.log("[SakuraMMD] playMotion", payload);
      // TODO: 按名称播放 VMD 动作
    }

    stopMotion(payload) {
      console.log("[SakuraMMD] stopMotion", payload);
      // TODO: 停止动作
    }

    setExpression(payload) {
      var name = payload && payload.name;
      var weight = (payload && typeof payload.weight === "number") ? payload.weight : 1.0;
      var runtime = window.SakuraMMDRuntime;
      if (!runtime || typeof runtime.setExpression !== "function") return;

      // 查角色配置里的「表情名→{morph:权重}」映射；未配置或 neutral 则清空表情组回中性。
      var expressions = (this.characterConfig && this.characterConfig.expressions) || {};
      var morphs = expressions[name];
      if (!morphs) {
        if (name && name !== "neutral") {
          console.warn("[SakuraMMD] no expression mapping for: " + name);
        }
        runtime.setExpression({});
        return;
      }
      // 整体权重叠加到每个 morph 上。
      var merged = {};
      for (var morph in morphs) {
        if (!Object.prototype.hasOwnProperty.call(morphs, morph)) continue;
        merged[morph] = Number(morphs[morph]) * weight;
      }
      runtime.setExpression(merged);
    }

    setLipSync(payload) {
      this.lipSyncValue = Number((payload && payload.value) || 0);
      var runtime = window.SakuraMMDRuntime;
      if (!runtime || typeof runtime.setLipSync !== "function") return;
      // 口型 morph 名与强度来自角色配置；未配置则静默跳过（不影响其他能力）。
      var lip = (this.characterConfig && this.characterConfig.lipSync) || {};
      var morph = lip.morph;
      if (!morph) return;
      var strength = (typeof lip.strength === "number") ? lip.strength : 0.8;
      runtime.setLipSync(this.lipSyncValue, morph, strength);
    }

    lookAt(payload) {
      var runtime = window.SakuraMMDRuntime;
      if (!runtime || typeof runtime.lookAt !== "function") return;
      var x = (payload && typeof payload.x === "number") ? payload.x : 0;
      var y = (payload && typeof payload.y === "number") ? payload.y : 0;
      runtime.lookAt(x, y);
    }

    setScale(payload) {
      var runtime = window.SakuraMMDRuntime;
      if (!runtime || typeof runtime.setScale !== "function") return;
      var scale = (payload && typeof payload.scale === "number") ? payload.scale : 1.0;
      runtime.setScale(scale);
    }

    handleEvent(payload) {
      // 仅记录。TTS/LLM → 口型/表情 的默认映射已由 Python 端 renderer.handle_event
      // 显式下发（set_lip_sync / set_expression），此处不再重复，避免双重数据源；
      // 保留本方法作为 web 侧自定义事件响应的扩展点。
      console.log("[SakuraMMD] handleEvent", payload && payload.name);
    }
  }

  // 经典脚本下挂到全局，供 main.js 实例化。
  window.SakuraMMD = SakuraMMD;
})();
