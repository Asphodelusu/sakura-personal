export const SCREEN_AWARENESS_POLL_INTERVAL_MS = 10_000;
export const FOCUS_OBSERVER_POLL_INTERVAL_MS = 5_000;
export const SCREEN_AWARENESS_PROMPT = "这是一次由 Sakura 定时截图触发的主动屏幕观察。以下截图按时间顺序展示我最近正在做的事情。请结合最近聊天历史和这些截图，以当前角色的语气自然接话：可以评论变化、接续任务、询问卡点或提供轻量帮助。不要逐张复述，也不要因为时间或久坐机械地提醒休息；如果没有明显变化，就简短说出你能确认的具体内容。";

const RESOLUTIONS = new Set(["fullscreen", "720p", "1080p", "2160p"]);
const QUIET_SKIP_CODES = new Set([
  "SCREEN_OBSERVATION_PRIVACY_BLOCKED",
  "SCREEN_OBSERVATION_SELF",
  "SCREEN_OBSERVATION_TARGET_STALE",
  "SCREEN_OBSERVATION_TARGET_UNAVAILABLE",
]);

export function createFocusAdvanceCaller({ invoke, generationId } = {}) {
  if (typeof invoke !== "function") {
    throw new Error("SCREEN_AWARENESS_DEPENDENCY_INVALID");
  }
  return ({ busy = false, outcome, scope } = {}) => {
    const fixedScope = scope === undefined || scope === null
      ? String(typeof generationId === "function" ? generationId() : "")
      : String(scope);
    const payload = { busy: busy === true, scope: fixedScope };
    if (outcome) payload.outcome = String(outcome);
    return invoke("observer_focus_advance", { payload });
  };
}

const PERSONAL_BOUNDS = Object.freeze({
  timerSeconds: [1, 86400],
  cooldownSeconds: [0, 86400],
  focusSettleDelay: [0, 3600],
  windowSwitchCooldown: [0, 86400],
  pollIntervalSeconds: [0.2, 300],
});

function isPersonalSettings(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.hasOwn(value, "timerSeconds")
    && !Object.hasOwn(value, "checkIntervalMinutes"));
}

function normalizePersonalSettings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || typeof value.enabled !== "boolean") {
    throw new Error("SCREEN_AWARENESS_SETTINGS_INVALID");
  }
  const settings = { enabled: value.enabled };
  for (const [key, bounds] of Object.entries(PERSONAL_BOUNDS)) {
    const number = Number(value[key]);
    if (!Number.isFinite(number) || number < bounds[0] || number > bounds[1]) {
      throw new Error("SCREEN_AWARENESS_SETTINGS_INVALID");
    }
    settings[key] = number;
  }
  if (Object.keys(value).some((key) => key !== "enabled" && !Object.hasOwn(PERSONAL_BOUNDS, key))) {
    throw new Error("SCREEN_AWARENESS_SETTINGS_INVALID");
  }
  return Object.freeze(settings);
}

export function normalizeScreenAwarenessSettings(value) {
  if (isPersonalSettings(value)) return normalizePersonalSettings(value);
  const settings = {
    enabled: value?.enabled === true,
    checkIntervalMinutes: Number(value?.checkIntervalMinutes),
    cooldownMinutes: Number(value?.cooldownMinutes),
    batchLimit: Number(value?.batchLimit),
    resolution: String(value?.resolution || ""),
  };
  if (!Number.isSafeInteger(settings.checkIntervalMinutes)
      || settings.checkIntervalMinutes < 1 || settings.checkIntervalMinutes > 120
      || !Number.isSafeInteger(settings.cooldownMinutes)
      || settings.cooldownMinutes < 1 || settings.cooldownMinutes > 120
      || !Number.isSafeInteger(settings.batchLimit)
      || settings.batchLimit < 1 || settings.batchLimit > 20
      || !RESOLUTIONS.has(settings.resolution)) {
    throw new Error("SCREEN_AWARENESS_SETTINGS_INVALID");
  }
  return Object.freeze(settings);
}

export function createScreenAwarenessController({
  invoke,
  send,
  isIdle,
  generationId,
  now = () => Date.now(),
  setInterval = (callback, delay) => globalThis.setInterval(callback, delay),
  clearInterval = (timer) => globalThis.clearInterval(timer),
  onDiagnostic = () => {},
  advanceFocus = null,
} = {}) {
  if ([invoke, send, isIdle, generationId].some((value) => typeof value !== "function")) {
    throw new Error("SCREEN_AWARENESS_DEPENDENCY_INVALID");
  }
  let settings = null;
  let timer = null;
  let disposed = false;
  let ticking = false;
  let generation = "";
  let epoch = 0;
  let lastActivityAt = now();
  let lastCaptureAt = now();
  let batchStartedAt = null;
  let batchCount = 0;

  function invokeBestEffort(command, args) {
    try { void Promise.resolve(invoke(command, args)).catch(() => {}); }
    catch { /* Native teardown may already have started. */ }
  }

  function resetClock(timestamp = now()) {
    lastActivityAt = timestamp;
    lastCaptureAt = timestamp;
    batchStartedAt = null;
    batchCount = 0;
  }

  function clearBatch(reason, timestamp = now()) {
    invokeBestEffort("clear_screen_awareness_batch");
    resetClock(timestamp);
    onDiagnostic("screen_awareness.batch.cleared", { reason });
  }

  function invalidate(reason, timestamp = now()) {
    epoch += 1;
    clearBatch(reason, timestamp);
  }

  function isCurrent(token, startedGeneration) {
    return token === epoch
      && !disposed
      && Boolean(settings?.enabled)
      && String(generationId() || "") === generation
      && generation === startedGeneration;
  }

  let attemptScope = "";
  let started = false;

  function personalMode() {
    return Boolean(settings && Object.hasOwn(settings, "timerSeconds"));
  }

  function pollDelay() {
    if (personalMode() && Number.isFinite(settings.pollIntervalSeconds)) {
      return settings.pollIntervalSeconds * 1000;
    }
    return typeof advanceFocus === "function"
      ? FOCUS_OBSERVER_POLL_INTERVAL_MS
      : SCREEN_AWARENESS_POLL_INTERVAL_MS;
  }

  function armTimer() {
    if (timer !== null) clearInterval(timer);
    timer = null;
    if (disposed || !started) return;
    if (personalMode() && typeof advanceFocus !== "function") return;
    timer = setInterval(() => { void tick(); }, pollDelay());
  }

  async function report(outcome) {
    if (typeof advanceFocus !== "function" || !outcome) return;
    try { await advanceFocus({ busy: !isIdle(), outcome, scope: attemptScope }); }
    catch { /* The capture attempt is already over. */ }
  }

  function discardOwnedWork(startedGeneration, attachmentId) {
    if (String(generationId() || "") !== startedGeneration || generation !== startedGeneration) return;
    if (attachmentId) {
      invokeBestEffort("release_screen_attachment", { payload: { attachmentId } });
    }
    clearBatch("stale_attempt");
  }

  async function fail(stage, error, attachmentId = null) {
    if (attachmentId) {
      invokeBestEffort("release_screen_attachment", { payload: { attachmentId } });
    }
    clearBatch(stage);
    onDiagnostic("screen_awareness.failed", { stage, code: String(error || stage).split("|")[0] });
  }

  function quietOutcome(code) {
    if (code === "SCREEN_OBSERVATION_PRIVACY_BLOCKED") return "privacy";
    if (code === "SCREEN_OBSERVATION_SELF") return "self";
    if (code === "SCREEN_OBSERVATION_TARGET_STALE" || code === "SCREEN_OBSERVATION_TARGET_UNAVAILABLE") return "aborted";
    return "";
  }

  async function tick() {
    if (disposed || ticking || !settings) return;
    const currentGeneration = String(generationId() || "");
    if (currentGeneration !== generation) {
      generation = currentGeneration;
      invalidate("generation_changed");
      return;
    }
    if (!settings.enabled || !currentGeneration) return;
    if (personalMode() && typeof advanceFocus !== "function") {
      onDiagnostic("screen_awareness.settings.unavailable", {
        code: "SCREEN_AWARENESS_SETTINGS_UNAVAILABLE",
      });
      return;
    }
    const token = epoch;
    const startedGeneration = generation;
    attemptScope = startedGeneration;
    ticking = true;
    let captured = false;
    try {
      const timestamp = now();
      let immediate = false;
      let captureTicket = null;
      if (typeof advanceFocus === "function") {
        let decision;
        try {
          decision = await advanceFocus({ busy: !isIdle(), scope: startedGeneration });
        } catch (error) {
          onDiagnostic("screen_awareness.focus.failed", {
            code: String(error instanceof Error ? error.message : error || "focus").split(/[|:]/)[0].trim(),
          });
          return;
        }
        if (!decision || decision.action !== "capture") return;
        if (personalMode()) {
          if (!/^[0-9a-f]{32}$/.test(String(decision.captureTicket || ""))) {
            onDiagnostic("screen_awareness.focus.failed", { code: "FOCUS_CAPTURE_TICKET_INVALID" });
            await report("aborted");
            return;
          }
          captureTicket = decision.captureTicket;
        }
        if (!isCurrent(token, startedGeneration) || !isIdle()) {
          await report("aborted");
          return;
        }
        immediate = true;
      } else if (!isIdle()) return;
      if (!isCurrent(token, startedGeneration)) {
        await report("aborted");
        return;
      }
      const batchLimit = personalMode() ? 1 : settings.batchLimit;
      const resolution = personalMode() ? "fullscreen" : settings.resolution;
      const intervalMs = personalMode() ? 0 : settings.checkIntervalMinutes * 60_000;
      const due = personalMode()
        ? immediate
        : immediate || (timestamp - lastActivityAt >= intervalMs && timestamp - lastCaptureAt >= intervalMs);
      if (due) {
        try {
          const result = await invoke("capture_screen_awareness_frame", { payload: {
            resolution,
            batchLimit,
            ...(personalMode() ? { scope: startedGeneration, captureTicket } : {}),
          } });
          if (!isCurrent(token, startedGeneration)) {
            discardOwnedWork(startedGeneration, null);
            await report("aborted");
            return;
          }
          if (!Number.isSafeInteger(result?.count) || result.count < 1 || result.count > batchLimit) {
            throw new Error("SCREEN_AWARENESS_CAPTURE_RESPONSE_INVALID");
          }
          captured = true;
          lastCaptureAt = timestamp;
          if (batchCount === 0) batchStartedAt = timestamp;
          batchCount = result.count;
        } catch (error) {
          const code = String(error instanceof Error ? error.message : error || "").split(/[|:]/)[0].trim();
          const quiet = quietOutcome(code);
          if (quiet) {
            lastCaptureAt = timestamp;
            onDiagnostic("screen_awareness.capture.skipped", { code });
            await report(quiet);
            return;
          }
          await report("failed");
          if (isCurrent(token, startedGeneration)) await fail("capture", error);
          return;
        }
      }
      if (!isCurrent(token, startedGeneration) || !isIdle()) {
        if (captured) discardOwnedWork(startedGeneration, null);
        await report("aborted");
        return;
      }
      if (!personalMode() && !immediate && (batchCount === 0 || batchStartedAt === null
          || (batchCount < settings.batchLimit
            && timestamp - batchStartedAt < settings.cooldownMinutes * 60_000))) return;
      if (immediate && batchCount === 0) return;

      let attachmentId = null;
      try {
        const attached = await invoke("attach_screen_awareness_batch", personalMode()
          ? { payload: { scope: startedGeneration, captureTicket } } : undefined);
        attachmentId = String(attached?.attachmentId || "");
        if (!isCurrent(token, startedGeneration) || !isIdle()) {
          discardOwnedWork(startedGeneration, attachmentId);
          await report("aborted");
          return;
        }
        if (!/^screen-[0-9a-f]{32}$/.test(attachmentId) || attached?.count !== batchCount) {
          throw new Error("SCREEN_AWARENESS_ATTACHMENT_RESPONSE_INVALID");
        }
        await send({ message: SCREEN_AWARENESS_PROMPT, attachmentId });
        if (!isCurrent(token, startedGeneration)) {
          discardOwnedWork(startedGeneration, attachmentId);
          await report("aborted");
          return;
        }
        resetClock(timestamp);
        await report("submitted");
      } catch (error) {
        await report("failed");
        if (isCurrent(token, startedGeneration)) await fail("send", error, attachmentId);
        else discardOwnedWork(startedGeneration, attachmentId);
      }
    } finally {
      ticking = false;
    }
  }

  return Object.freeze({
    applySettings(value) {
      settings = normalizeScreenAwarenessSettings(value);
      generation = String(generationId() || "");
      invalidate(settings.enabled ? "settings_changed" : "disabled");
      if (personalMode() && typeof advanceFocus !== "function") {
        onDiagnostic("screen_awareness.settings.unavailable", {
          code: "SCREEN_AWARENESS_SETTINGS_UNAVAILABLE",
        });
        if (timer !== null) clearInterval(timer);
        timer = null;
        return;
      }
      if (started) armTimer();
    },
    start() {
      if (disposed || started) return;
      started = true;
      armTimer();
    },
    tick,
    noteActivity() {
      lastActivityAt = now();
    },
    noteManualSend() {
      invalidate("manual_send");
    },
    generationChanged(value = generationId()) {
      generation = String(value || "");
      invalidate("generation_changed");
    },
    snapshot() {
      return Object.freeze({ settings, generation, lastActivityAt, lastCaptureAt, batchStartedAt, batchCount });
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (timer !== null) clearInterval(timer);
      timer = null;
      invalidate("dispose");
    },
  });
}
