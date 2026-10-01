import assert from "node:assert/strict";
import test from "node:test";

import {
  SCREEN_AWARENESS_PROMPT,
  createFocusAdvanceCaller,
  createScreenAwarenessController,
} from "../chat/screen-awareness-controller.js";

function settings(overrides = {}) {
  return {
    enabled: true,
    checkIntervalMinutes: 1,
    cooldownMinutes: 2,
    batchLimit: 3,
    resolution: "1080p",
    ...overrides,
  };
}

function harness({ enabled = true, overrides = {}, advanceFocus = null, beforeCapture = null } = {}) {
  let clock = 0;
  let idle = true;
  let generation = "generation-a";
  let captureCount = 0;
  let sendFailure = false;
  const captureErrors = [];
  const calls = [];
  const sends = [];
  const controller = createScreenAwarenessController({
    now: () => clock,
    generationId: () => generation,
    isIdle: () => idle,
    invoke: async (command, args) => {
      calls.push([command, args]);
      if (command === "capture_screen_awareness_frame") {
        if (beforeCapture) await beforeCapture;
        const error = captureErrors.shift();
        if (error) throw new Error(error);
        return { count: ++captureCount, droppedCount: 0 };
      }
      if (command === "attach_screen_awareness_batch") {
        return { attachmentId: `screen-${"a".repeat(32)}`, count: captureCount };
      }
      return true;
    },
    send: async (payload) => {
      sends.push(payload);
      if (sendFailure) throw new Error("CHAT_FAILED");
      return { operationId: "op-1" };
    },
    setInterval: () => 1,
    clearInterval: () => {},
    advanceFocus,
  });
  controller.applySettings(settings({ enabled, ...overrides }));
  return {
    controller,
    calls,
    sends,
    captureErrors,
    setClock: (value) => { clock = value; },
    setIdle: (value) => { idle = value; },
    setGeneration: (value) => { generation = value; },
    failSend: () => { sendFailure = true; },
    resetCaptures: () => { captureCount = 0; },
  };
}

function commands(env, name) {
  return env.calls.filter(([command]) => command === name);
}

test("disabled screen awareness never captures", async () => {
  const env = harness({ enabled: false });
  env.setClock(10 * 60_000);
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 0);
});

test("capture interval and first-frame cooldown produce one ordered ordinary chat send", async () => {
  const env = harness();
  env.setClock(60_000);
  await env.controller.tick();
  env.setClock(120_000);
  await env.controller.tick();
  assert.equal(env.sends.length, 0);
  env.setClock(180_000);
  await env.controller.tick();

  assert.equal(commands(env, "capture_screen_awareness_frame").length, 3);
  assert.equal(commands(env, "attach_screen_awareness_batch").length, 1);
  assert.deepEqual(env.sends, [{
    message: SCREEN_AWARENESS_PROMPT,
    attachmentId: `screen-${"a".repeat(32)}`,
  }]);
});

test("busy state, fresh input, and long sleep skip work without catch-up", async () => {
  const env = harness();
  env.setClock(60_000);
  env.setIdle(false);
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 0);

  env.setIdle(true);
  env.controller.noteActivity();
  env.setClock(119_999);
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 0);

  env.setClock(8 * 60 * 60_000);
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 1);
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 1);
});

test("manual send, hot settings, generation change, and dispose clear the native batch", async () => {
  const env = harness();
  const before = commands(env, "clear_screen_awareness_batch").length;
  env.controller.noteManualSend();
  env.controller.applySettings(settings({ resolution: "720p" }));
  env.setGeneration("generation-b");
  await env.controller.tick();
  env.controller.dispose();
  assert.equal(commands(env, "clear_screen_awareness_batch").length, before + 4);
});

test("failed automatic send releases the attachment and does not retry", async () => {
  const env = harness();
  env.failSend();
  env.setClock(60_000);
  await env.controller.tick();
  env.setClock(180_000);
  await env.controller.tick();
  await Promise.resolve();
  assert.equal(env.sends.length, 1);
  assert.equal(commands(env, "release_screen_attachment").length, 1);
  await env.controller.tick();
  assert.equal(env.sends.length, 1);
});

test("a full batch is sent at once instead of waiting for the cooldown", async () => {
  const env = harness({ overrides: { batchLimit: 1, cooldownMinutes: 10 } });
  env.setClock(60_000);
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 1);
  assert.equal(env.sends.length, 1);
});

test("focus advance captures one frame when core asks and does not capture while held", async () => {
  const decisions = [
    { action: "hold", trigger: "window", reason: "busy" },
    { action: "capture", trigger: "window", reason: "ready" },
  ];
  const seen = [];
  const env = harness({
    advanceFocus: async ({ busy }) => {
      seen.push(busy);
      return decisions.shift();
    },
  });
  env.setIdle(false);
  await env.controller.tick();
  assert.deepEqual(seen, [true]);
  assert.equal(env.sends.length, 0);
  env.setIdle(true);
  await env.controller.tick();
  assert.equal(env.sends.length, 1);
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 1);
});

test("a deferred focus decision does not capture after invalidation", async () => {
  for (const invalidate of ["disable", "dispose", "generation", "manual"]) {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const outcomes = [];
    const env = harness({
      advanceFocus: async ({ outcome }) => {
        if (outcome) {
          outcomes.push(outcome);
          return { action: "wait", trigger: "", reason: outcome };
        }
        return gate;
      },
    });
    const pending = env.controller.tick();
    if (invalidate === "disable") env.controller.applySettings(settings({ enabled: false }));
    if (invalidate === "dispose") env.controller.dispose();
    if (invalidate === "generation") env.controller.generationChanged("generation-b");
    if (invalidate === "manual") env.controller.noteManualSend();
    release({ action: "capture", trigger: "window", reason: "ready" });
    await pending;
    assert.equal(commands(env, "capture_screen_awareness_frame").length, 0, invalidate);
    assert.equal(env.sends.length, 0, invalidate);
    assert.deepEqual(outcomes, ["aborted"], invalidate);
  }
});

test("a deferred capture is not sent and does not clear the next generation", async () => {
  let releaseCapture;
  const captureGate = new Promise((resolve) => { releaseCapture = resolve; });
  const env = harness({
    beforeCapture: captureGate,
    advanceFocus: ({ outcome }) => (
      outcome
        ? { action: "wait", trigger: "", reason: outcome }
        : { action: "capture", trigger: "window", reason: "ready" }
    ),
  });
  const pending = env.controller.tick();
  await Promise.resolve();
  env.controller.generationChanged("generation-b");
  const clearsAfterSwitch = commands(env, "clear_screen_awareness_batch").length;
  releaseCapture();
  await pending;
  assert.equal(env.sends.length, 0);
  assert.equal(commands(env, "clear_screen_awareness_batch").length, clearsAfterSwitch);
  env.setGeneration("generation-b");
  env.controller.generationChanged("generation-b");
  const clearsBeforeNext = commands(env, "clear_screen_awareness_batch").length;
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 2);
  assert.equal(commands(env, "clear_screen_awareness_batch").length, clearsBeforeNext);
});

test("production focus caller keeps the supplied scope and outcome", async () => {
  const calls = [];
  let current = "generation-new";
  const advanceFocus = createFocusAdvanceCaller({
    invoke: async (command, args) => {
      calls.push([command, args]);
      return { action: "wait", trigger: "", reason: "aborted" };
    },
    generationId: () => current,
  });
  await advanceFocus({ busy: false, scope: "generation-old", outcome: "aborted" });
  assert.deepEqual(calls, [[
    "observer_focus_advance",
    { payload: { busy: false, scope: "generation-old", outcome: "aborted" } },
  ]]);
});

test("a late settlement still names the generation that started the attempt", async () => {
  const payloads = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const env = harness({
    advanceFocus: (request) => {
      payloads.push({ ...request });
      if (request.outcome) return { action: "wait", trigger: "", reason: request.outcome };
      return gate;
    },
  });
  const pending = env.controller.tick();
  env.setGeneration("generation-b");
  env.controller.generationChanged("generation-b");
  release({ action: "capture", trigger: "window", reason: "ready" });
  await pending;
  assert.equal(payloads.at(-1).scope, "generation-a");
  assert.equal(payloads.at(-1).outcome, "aborted");
});

test("private, own, or stale target screens are skipped quietly until the next interval", async () => {
  for (const code of [
    "SCREEN_OBSERVATION_PRIVACY_BLOCKED",
    "SCREEN_OBSERVATION_SELF",
    "SCREEN_OBSERVATION_TARGET_STALE",
  ]) {
    const env = harness({ overrides: { batchLimit: 1 } });
    const clearsBefore = commands(env, "clear_screen_awareness_batch").length;
    env.captureErrors.push(code);
    env.setClock(60_000);
    await env.controller.tick();
    assert.equal(env.sends.length, 0, code);
    assert.equal(commands(env, "clear_screen_awareness_batch").length, clearsBefore, code);
    env.setClock(90_000);
    await env.controller.tick();
    assert.equal(commands(env, "capture_screen_awareness_frame").length, 1, code);
    env.setClock(120_000);
    await env.controller.tick();
    assert.equal(commands(env, "capture_screen_awareness_frame").length, 2, code);
    assert.equal(env.sends.length, 1, code);
  }
});

test("personal polling keeps fractional seconds and does not use the minute scheduler", async () => {
  const personal = {
    enabled: true,
    timerSeconds: 40.5,
    cooldownSeconds: 600,
    focusSettleDelay: 15,
    windowSwitchCooldown: 60,
    pollIntervalSeconds: 2.5,
  };
  const diagnostics = [];
  const quietCalls = [];
  const quiet = createScreenAwarenessController({
    now: () => 0,
    generationId: () => "generation-a",
    isIdle: () => true,
    invoke: async (command) => {
      quietCalls.push(command);
      return { count: 1 };
    },
    send: async () => {},
    setInterval: () => 1,
    clearInterval: () => {},
    onDiagnostic: (event) => diagnostics.push(event),
  });
  quiet.applySettings(personal);
  quiet.start();
  await quiet.tick();
  assert.equal(diagnostics.includes("screen_awareness.settings.unavailable"), true);
  assert.equal(quietCalls.includes("capture_screen_awareness_frame"), false);

  const polled = [];
  const focused = [];
  const controller = createScreenAwarenessController({
    now: () => 0,
    generationId: () => "generation-a",
    isIdle: () => true,
    invoke: async (command, args) => {
      focused.push([command, args]);
      if (command === "capture_screen_awareness_frame") return { count: 1 };
      if (command === "attach_screen_awareness_batch") {
        return { attachmentId: `screen-${"a".repeat(32)}`, count: 1 };
      }
      return true;
    },
    send: async () => ({ operationId: "op-1" }),
    setInterval: (_callback, delay) => {
      polled.push(delay);
      return 2;
    },
    clearInterval: () => {},
    advanceFocus: async () => ({ action: "capture", trigger: "timer", reason: "ready", captureTicket: "b".repeat(32) }),
  });
  controller.applySettings(personal);
  controller.start();
  assert.deepEqual(polled, [2500]);
  await controller.tick();
  const capture = focused.find(([command]) => command === "capture_screen_awareness_frame");
  assert.equal(capture[1].payload.batchLimit, 1);
  assert.equal(capture[1].payload.resolution, "fullscreen");
  assert.equal(capture[1].payload.scope, "generation-a");
  assert.equal(capture[1].payload.captureTicket, "b".repeat(32));
  assert.deepEqual(focused.find(([command]) => command === "attach_screen_awareness_batch")[1], {
    payload: { scope: "generation-a", captureTicket: "b".repeat(32) },
  });
});

test("personal observation refuses a capture without its window offer ticket", async () => {
  const env = harness({ advanceFocus: async () => ({ action: "capture", trigger: "window", reason: "ready" }) });
  env.controller.applySettings({ enabled: true, timerSeconds: 480, cooldownSeconds: 600,
    focusSettleDelay: 15, windowSwitchCooldown: 60, pollIntervalSeconds: 5 });
  await env.controller.tick();
  assert.equal(commands(env, "capture_screen_awareness_frame").length, 0);
  assert.equal(env.sends.length, 0);
});
