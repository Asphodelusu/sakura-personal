import assert from "node:assert/strict";
import test from "node:test";

import {
  createScreenAwarenessSettingsController,
  validateScreenAwarenessSnapshot,
} from "../settings/screen-awareness-runtime.js";

function snapshot(settings = {}) {
  return {
    schemaVersion: 1,
    settings: {
      // Tauri/serde_json may project map keys in lexical order. Dirty tracking must compare
      // values rather than depending on the insertion order received from the transport.
      batchLimit: 6,
      checkIntervalMinutes: 20,
      cooldownMinutes: 10,
      enabled: true,
      resolution: "fullscreen",
      ...settings,
    },
    windowGeneration: 3,
    coreGenerationId: "generation-a",
  };
}

function control() {
  const listeners = {};
  return {
    value: "",
    checked: false,
    disabled: false,
    min: "",
    max: "",
    addEventListener(name, handler) { listeners[name] = handler; },
    fire(name) { listeners[name]?.(); },
  };
}

function personalSnapshot(settings = {}) {
  return {
    schemaVersion: 1,
    settings: {
      cooldownSeconds: 600,
      enabled: true,
      focusSettleDelay: 15,
      pollIntervalSeconds: 2.5,
      timerSeconds: 40.5,
      windowSwitchCooldown: 60,
      ...settings,
    },
    windowGeneration: 3,
    coreGenerationId: "generation-a",
  };
}

test("personal screen settings keep seconds and hide the periodic controls", async () => {
  const rows = [
    { getAttribute: () => "periodic", hidden: false },
    { getAttribute: () => "personal", hidden: true },
  ];
  const controls = {
    enabled: control(),
    checkInterval: control(),
    cooldown: control(),
    batchLimit: control(),
    screenResolution: control(),
    timerSeconds: control(),
    cooldownSeconds: control(),
    focusSettleDelay: control(),
    windowSwitchCooldown: control(),
    screenAwarenessHint: { hidden: false },
  };
  const calls = [];
  const controller = createScreenAwarenessSettingsController({
    document: {
      getElementById: (id) => controls[id],
      querySelectorAll: () => rows,
    },
    enhanceSelect() {},
    refreshSelect() {},
    onDirty() {},
    invoke: async (command, args) => {
      calls.push([command, args]);
      return personalSnapshot(args.settings);
    },
  });
  controller.initialize(personalSnapshot());
  assert.equal(rows[0].hidden, true);
  assert.equal(rows[1].hidden, false);
  assert.equal(controls.screenAwarenessHint.hidden, true);
  assert.equal(controls.timerSeconds.value, "40.5");
  assert.equal(controller.isDirty(), false);
  controls.timerSeconds.value = "90.5";
  assert.equal(controller.isDirty(), true);
  await controller.save();
  assert.equal(calls[0][1].settings.timerSeconds, 90.5);
  assert.equal(calls[0][1].settings.pollIntervalSeconds, 2.5);
  assert.equal(calls[0][1].settings.checkIntervalMinutes, undefined);
  assert.throws(() => validateScreenAwarenessSnapshot(personalSnapshot({ timerSeconds: 0 })));
});

test("screen awareness settings are exact and bounded", () => {
  assert.equal(validateScreenAwarenessSnapshot(snapshot()).settings.checkIntervalMinutes, 20);
  assert.throws(() => validateScreenAwarenessSnapshot(snapshot({ batchLimit: 21 })));
  assert.throws(() => validateScreenAwarenessSnapshot({ ...snapshot(), privatePath: "x" }));
});

test("screen awareness settings save both preserves identity and rebases immediately", async () => {
  const controls = {
    enabled: control(),
    checkInterval: control(),
    cooldown: control(),
    batchLimit: control(),
    screenResolution: control(),
  };
  const calls = [];
  const enhancedSelects = [];
  const refreshedSelects = [];
  const controller = createScreenAwarenessSettingsController({
    document: { getElementById: (id) => controls[id] },
    enhanceSelect: (select) => enhancedSelects.push(select),
    refreshSelect: (select) => refreshedSelects.push(select),
    onDirty: () => {},
    invoke: async (command, args) => {
      calls.push([command, args]);
      return snapshot({ ...args.settings, resolution: args.settings.resolution });
    },
  });
  controller.initialize(snapshot());
  assert.deepEqual(enhancedSelects, [controls.screenResolution]);
  assert.equal(refreshedSelects.includes(controls.screenResolution), true);
  assert.equal(controller.isDirty(), false);
  controls.checkInterval.value = "25";
  controls.checkInterval.fire("input");
  assert.equal(controller.isDirty(), true);
  await controller.save();
  assert.equal(calls[0][0], "settings_screen_awareness_save");
  assert.equal(calls[0][1].windowGeneration, 3);
  assert.equal(calls[0][1].coreGenerationId, "generation-a");
  assert.equal(calls[0][1].settings.checkIntervalMinutes, 25);
  assert.equal(controller.isDirty(), false);
});

test("screen awareness rebind preserves its global draft and uses the new generation", async () => {
  const controls = {
    enabled: control(),
    checkInterval: control(),
    cooldown: control(),
    batchLimit: control(),
    screenResolution: control(),
  };
  const calls = [];
  const controller = createScreenAwarenessSettingsController({
    document: { getElementById: (id) => controls[id] },
    enhanceSelect() {},
    refreshSelect() {},
    onDirty() {},
    invoke: async (command, args) => {
      calls.push([command, args]);
      return snapshot({ ...args.settings });
    },
  });
  controller.initialize(snapshot());
  controls.checkInterval.value = "25";
  assert.equal(controller.isDirty(), true);

  controller.rebindIdentity("generation-b");
  assert.equal(controller.isDirty(), true);
  await controller.save();

  assert.equal(calls[0][1].coreGenerationId, "generation-b");
  assert.equal(calls[0][1].settings.checkIntervalMinutes, 25);
});
