// main.js — 入口：创建 SakuraMMD 控制器并初始化。
//
// 经典脚本（见 index.html 说明）。SakuraMMD 由 sakura_mmd.js 挂到全局。
(function () {
  "use strict";

  var canvas = document.getElementById("mmd-canvas");
  window.sakuraMMD = new window.SakuraMMD({ canvas: canvas });
  window.sakuraMMD.initialize();

  console.log("[SakuraMMD] controller ready");
})();
