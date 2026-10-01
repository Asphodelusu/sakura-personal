const SNAPSHOT_KEYS = Object.freeze([
  "schemaVersion",
  "settings",
  "windowGeneration",
  "coreGenerationId",
]);
const SETTINGS_KEYS = Object.freeze([
  "enabled",
  "checkIntervalMinutes",
  "cooldownMinutes",
  "batchLimit",
  "resolution",
]);
const PERSONAL_SETTINGS_KEYS = Object.freeze([
  "enabled",
  "timerSeconds",
  "cooldownSeconds",
  "focusSettleDelay",
  "windowSwitchCooldown",
  "pollIntervalSeconds",
]);
const PERSONAL_BOUNDS = Object.freeze({
  timerSeconds: [1, 86400],
  cooldownSeconds: [0, 86400],
  focusSettleDelay: [0, 3600],
  windowSwitchCooldown: [0, 86400],
  pollIntervalSeconds: [0.2, 300],
});
const RESOLUTIONS = new Set(["fullscreen", "720p", "1080p", "2160p"]);

function isPersonalSettings(settings) {
  return Boolean(settings && typeof settings === "object" && !Array.isArray(settings)
    && Object.hasOwn(settings, "timerSeconds")
    && !Object.hasOwn(settings, "checkIntervalMinutes"));
}

function exactKeys(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key)));
}

function finiteInRange(value, minimum, maximum) {
  return typeof value === "number" && Number.isFinite(value) && value >= minimum && value <= maximum;
}

function validatePersonalSettings(settings) {
  if (!exactKeys(settings, PERSONAL_SETTINGS_KEYS) || typeof settings.enabled !== "boolean") {
    throw new Error("invalid screen awareness settings");
  }
  for (const [key, bounds] of Object.entries(PERSONAL_BOUNDS)) {
    if (!finiteInRange(settings[key], bounds[0], bounds[1])) {
      throw new Error(`invalid screen awareness setting: ${key}`);
    }
  }
  return settings;
}

function validateSettings(settings) {
  if (isPersonalSettings(settings)) return validatePersonalSettings(settings);
  if (!exactKeys(settings, SETTINGS_KEYS) || typeof settings.enabled !== "boolean") {
    throw new Error("invalid screen awareness settings");
  }
  for (const [key, maximum] of [["checkIntervalMinutes", 120], ["cooldownMinutes", 120], ["batchLimit", 20]]) {
    const value = settings[key];
    if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
      throw new Error(`invalid screen awareness setting: ${key}`);
    }
  }
  if (!RESOLUTIONS.has(settings.resolution)) throw new Error("invalid screen awareness resolution");
  return settings;
}

export function validateScreenAwarenessSnapshot(input) {
  if (!exactKeys(input, SNAPSHOT_KEYS) || input.schemaVersion !== 1) {
    throw new Error("invalid screen awareness snapshot");
  }
  if (!Number.isSafeInteger(input.windowGeneration) || input.windowGeneration < 1) {
    throw new Error("invalid screen awareness window generation");
  }
  if (typeof input.coreGenerationId !== "string" || !input.coreGenerationId) {
    throw new Error("invalid screen awareness Core generation");
  }
  validateSettings(input.settings);
  return Object.freeze({ ...input, settings: Object.freeze({ ...input.settings }) });
}

export function createScreenAwarenessSettingsController({
  document,
  invoke,
  enhanceSelect = () => {},
  refreshSelect = () => {},
  onDirty,
}) {
  const controls = {
    enabled: document.getElementById("enabled"),
    checkIntervalMinutes: document.getElementById("checkInterval"),
    cooldownMinutes: document.getElementById("cooldown"),
    batchLimit: document.getElementById("batchLimit"),
    resolution: document.getElementById("screenResolution"),
    timerSeconds: document.getElementById("timerSeconds"),
    cooldownSeconds: document.getElementById("cooldownSeconds"),
    focusSettleDelay: document.getElementById("focusSettleDelay"),
    windowSwitchCooldown: document.getElementById("windowSwitchCooldown"),
  };
  let snapshot = null;
  let baseline = null;
  let personalMode = false;

  enhanceSelect(controls.resolution);

  function showMode(personal) {
    personalMode = personal;
    if (typeof document.querySelectorAll === "function") {
      for (const row of document.querySelectorAll("[data-screen-awareness]")) {
        const kind = row.getAttribute("data-screen-awareness");
        row.hidden = personal ? kind !== "personal" : kind === "personal";
      }
    }
    const hint = document.getElementById("screenAwarenessHint");
    if (hint) hint.hidden = personal;
  }

  function syncEnabled() {
    const keys = personalMode
      ? ["timerSeconds", "cooldownSeconds", "focusSettleDelay", "windowSwitchCooldown"]
      : ["checkIntervalMinutes", "cooldownMinutes", "batchLimit", "resolution"];
    for (const key of keys) {
      if (controls[key]) controls[key].disabled = !controls.enabled.checked;
    }
    if (!personalMode) refreshSelect(controls.resolution);
  }

  function read() {
    if (personalMode) {
      return validatePersonalSettings({
        enabled: controls.enabled.checked,
        timerSeconds: Number(controls.timerSeconds.value),
        cooldownSeconds: Number(controls.cooldownSeconds.value),
        focusSettleDelay: Number(controls.focusSettleDelay.value),
        windowSwitchCooldown: Number(controls.windowSwitchCooldown.value),
        pollIntervalSeconds: snapshot.settings.pollIntervalSeconds,
      });
    }
    return validateSettings({
      enabled: controls.enabled.checked,
      checkIntervalMinutes: Number.parseInt(controls.checkIntervalMinutes.value, 10),
      cooldownMinutes: Number.parseInt(controls.cooldownMinutes.value, 10),
      batchLimit: Number.parseInt(controls.batchLimit.value, 10),
      resolution: controls.resolution.value,
    });
  }

  function fill(settings) {
    controls.enabled.checked = settings.enabled;
    if (isPersonalSettings(settings)) {
      showMode(true);
      for (const [key, bounds] of Object.entries(PERSONAL_BOUNDS)) {
        if (!controls[key]) continue;
        controls[key].min = String(bounds[0]);
        controls[key].max = String(bounds[1]);
        controls[key].value = String(settings[key]);
      }
      syncEnabled();
      return;
    }
    showMode(false);
    controls.checkIntervalMinutes.min = "1";
    controls.checkIntervalMinutes.max = "120";
    controls.checkIntervalMinutes.value = String(settings.checkIntervalMinutes);
    controls.cooldownMinutes.min = "1";
    controls.cooldownMinutes.max = "120";
    controls.cooldownMinutes.value = String(settings.cooldownMinutes);
    controls.batchLimit.min = "1";
    controls.batchLimit.max = "20";
    controls.batchLimit.value = String(settings.batchLimit);
    controls.resolution.value = settings.resolution;
    syncEnabled();
  }

  function initialize(input) {
    snapshot = validateScreenAwarenessSnapshot(input);
    baseline = { ...snapshot.settings };
    fill(baseline);
    onDirty();
  }

  for (const control of Object.values(controls)) {
    if (!control) continue;
    control.addEventListener("input", onDirty);
    control.addEventListener("change", () => {
      syncEnabled();
      onDirty();
    });
  }

  return Object.freeze({
    initialize,
    isDirty() {
      try {
        const current = read();
        const keys = personalMode ? PERSONAL_SETTINGS_KEYS : SETTINGS_KEYS;
        return Boolean(baseline && keys.some((key) => current[key] !== baseline[key]));
      }
      catch { return true; }
    },
    async save() {
      if (!snapshot) throw new Error("screen awareness settings are not initialized");
      const result = await invoke("settings_screen_awareness_save", {
        windowGeneration: snapshot.windowGeneration,
        coreGenerationId: snapshot.coreGenerationId,
        settings: read(),
      });
      initialize(result);
      return result;
    },
    rebindIdentity(coreGenerationId) {
      if (!snapshot || typeof coreGenerationId !== "string" || !coreGenerationId) {
        throw new Error("invalid screen awareness Core generation");
      }
      snapshot = Object.freeze({ ...snapshot, coreGenerationId });
    },
    discard() {
      if (baseline) fill(baseline);
      onDirty();
    },
    dispose() {
      snapshot = null;
      baseline = null;
    },
  });
}
