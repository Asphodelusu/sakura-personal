// Proposes self-initiated turns. Core owns every decision: a proposal may end in silence.
export const INTIMACY_CONTINUE_DELAYS_MS = Object.freeze([20_000, 35_000, 60_000]);
export const RELATIONSHIP_PROPOSAL_INTERVAL_MS = 60_000;
export const INITIATIVE_RETRY_MS = 2_000;
export const INITIATIVE_POLL_MS = 1_000;

const TERMINALS = new Set(["chat.completed", "chat.failed", "chat.cancelled"]);
const TRANSIENT_DISPATCH_CODES = new Set([
  "CHAT_INTERACTION_ACTIVE",
  "CHAT_NOT_READY",
  "CHAT_BRIDGE_UNAVAILABLE",
  "CHAT_GENERATION_INVALIDATED",
  "CHAT_GENERATION_MISMATCH",
  "CHAT_DISPATCH_ABORTED",
  "CHAT_START_TIMEOUT",
]);

function errorCode(error) {
  const value = error instanceof Error ? error.message : error;
  return String(value || "CHAT_INITIATIVE_FAILED").split(/[|:]/)[0].trim();
}

function spoke(event) {
  return event?.type === "chat.completed"
    && Array.isArray(event.reply?.segments)
    && event.reply.segments.length > 0;
}

export function createInitiativeController({
  send,
  cancel,
  isIdle,
  now = () => Date.now(),
  setInterval = (callback, delay) => globalThis.setInterval(callback, delay),
  clearInterval = (timer) => globalThis.clearInterval(timer),
  onDiagnostic = () => {},
} = {}) {
  if ([send, cancel, isIdle].some((value) => typeof value !== "function")) {
    throw new Error("INITIATIVE_DEPENDENCY_INVALID");
  }
  let disposed = false;
  let timer = null;
  // null: no chain. Otherwise the index of the next continuation to propose.
  let continuationIndex = null;
  let continuationDueAt = null;
  let nextRelationshipAt = now() + RELATIONSHIP_PROPOSAL_INTERVAL_MS;
  let inFlight = null;
  let earlyTerminal = null;

  function endChain() {
    continuationIndex = null;
    continuationDueAt = null;
  }

  function deferRelationship() {
    nextRelationshipAt = now() + RELATIONSHIP_PROPOSAL_INTERVAL_MS;
  }

  function settle(event) {
    const current = inFlight;
    inFlight = null;
    if (current.kind === "intimacy_continue") {
      if (spoke(event) && continuationIndex !== null) {
        continuationIndex += 1;
        continuationDueAt = null;
        if (continuationIndex >= INTIMACY_CONTINUE_DELAYS_MS.length) endChain();
      } else {
        endChain();
      }
    } else {
      deferRelationship();
    }
    onDiagnostic("initiative.settled", { kind: current.kind, terminal: event.type, spoke: spoke(event) });
  }

  async function dispatch(kind) {
    const current = { kind, operationId: null, cancelRequested: false };
    inFlight = current;
    try {
      const response = await send(kind);
      if (inFlight !== current) return;
      current.operationId = String(response?.operationId || "");
      if (!current.operationId) throw new Error("CHAT_INITIATIVE_RESPONSE_INVALID");
      if (earlyTerminal?.operationId === current.operationId) settle(earlyTerminal);
      else if (current.cancelRequested) void cancel(current.operationId);
    } catch (error) {
      if (inFlight !== current) return;
      inFlight = null;
      const code = errorCode(error);
      const transient = TRANSIENT_DISPATCH_CODES.has(code);
      if (kind === "intimacy_continue" && transient && continuationIndex !== null) {
        continuationDueAt = now() + INITIATIVE_RETRY_MS;
      } else if (kind === "intimacy_continue") {
        endChain();
      } else {
        deferRelationship();
      }
      onDiagnostic("initiative.dispatch_failed", { kind, code });
    } finally {
      earlyTerminal = null;
    }
  }

  async function tick() {
    if (disposed || inFlight || !isIdle()) return;
    const timestamp = now();
    if (continuationIndex !== null) {
      if (continuationDueAt === null) {
        continuationDueAt = timestamp + INTIMACY_CONTINUE_DELAYS_MS[continuationIndex];
        return;
      }
      if (timestamp >= continuationDueAt) await dispatch("intimacy_continue");
      return;
    }
    if (timestamp >= nextRelationshipAt) await dispatch("relationship_initiative");
  }

  return Object.freeze({
    start() {
      if (disposed || timer !== null) return;
      timer = setInterval(() => { void tick(); }, INITIATIVE_POLL_MS);
    },
    tick,
    handleChatEvent(event) {
      if (!TERMINALS.has(event?.type)) return;
      if (inFlight) {
        if (!inFlight.operationId) {
          if (typeof event.operationId === "string" && event.operationId) earlyTerminal = event;
          return;
        }
        if (event.operationId === inFlight.operationId) settle(event);
        return;
      }
      if (event.presentation === "interactive" && spoke(event)) {
        continuationIndex = 0;
        continuationDueAt = null;
      }
    },
    noteActivity() {
      endChain();
      deferRelationship();
      if (!inFlight || inFlight.cancelRequested) return;
      inFlight.cancelRequested = true;
      if (inFlight.operationId) void cancel(inFlight.operationId);
    },
    generationChanged() {
      inFlight = null;
      earlyTerminal = null;
      endChain();
      deferRelationship();
    },
    isPending() {
      return Boolean(inFlight);
    },
    dispose() {
      disposed = true;
      if (timer !== null) clearInterval(timer);
      timer = null;
      inFlight = null;
      earlyTerminal = null;
      endChain();
    },
  });
}
