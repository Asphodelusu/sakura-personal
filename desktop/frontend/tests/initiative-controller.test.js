import assert from "node:assert/strict";
import test from "node:test";

import {
  createInitiativeController,
  INITIATIVE_RETRY_MS,
  INTIMACY_CONTINUE_DELAYS_MS,
  RELATIONSHIP_PROPOSAL_INTERVAL_MS,
} from "../chat/initiative-controller.js";

function harness() {
  let timestamp = 0;
  let idle = true;
  let nextOperation = 0;
  const sends = [];
  const cancels = [];
  const sendResults = [];
  const controller = createInitiativeController({
    send: async (kind) => {
      sends.push(kind);
      const result = sendResults.shift();
      if (result instanceof Error) throw result;
      return result || { operationId: `op-${++nextOperation}` };
    },
    cancel: async (operationId) => { cancels.push(operationId); return true; },
    isIdle: () => idle,
    now: () => timestamp,
    setInterval: () => 1,
    clearInterval: () => {},
  });
  return {
    controller,
    sends,
    cancels,
    sendResults,
    setIdle(value) { idle = value; },
    advance(value) { timestamp += value; },
    lastOperation() { return `op-${nextOperation}`; },
    complete(operationId, { presentation = "silent", segments = [{ text: "x" }], type = "chat.completed" } = {}) {
      controller.handleChatEvent({ type, operationId, presentation, reply: { segments } });
    },
  };
}

test("relationship proposals wait for the interval and an idle desktop, one at a time", async () => {
  const env = harness();
  env.advance(RELATIONSHIP_PROPOSAL_INTERVAL_MS - 1);
  await env.controller.tick();
  assert.deepEqual(env.sends, []);

  env.advance(1);
  env.setIdle(false);
  await env.controller.tick();
  assert.deepEqual(env.sends, []);

  env.setIdle(true);
  await env.controller.tick();
  await env.controller.tick();
  assert.deepEqual(env.sends, ["relationship_initiative"]);

  env.complete(env.lastOperation(), { segments: [] });
  env.advance(RELATIONSHIP_PROPOSAL_INTERVAL_MS - 1);
  await env.controller.tick();
  assert.equal(env.sends.length, 1);
  env.advance(1);
  await env.controller.tick();
  assert.equal(env.sends.length, 2);
});

test("an interactive reply starts at most three continuations, each after an idle delay", async () => {
  const env = harness();
  env.complete("user-op", { presentation: "interactive" });
  for (const delay of INTIMACY_CONTINUE_DELAYS_MS) {
    await env.controller.tick();
    env.advance(delay - 1);
    await env.controller.tick();
    const before = env.sends.filter((kind) => kind === "intimacy_continue").length;
    env.advance(1);
    await env.controller.tick();
    assert.equal(env.sends.filter((kind) => kind === "intimacy_continue").length, before + 1);
    env.complete(env.lastOperation());
  }
  await env.controller.tick();
  env.advance(Math.max(...INTIMACY_CONTINUE_DELAYS_MS));
  await env.controller.tick();
  assert.equal(env.sends.filter((kind) => kind === "intimacy_continue").length, 3);
});

test("the delay only starts once playback has finished", async () => {
  const env = harness();
  env.complete("user-op", { presentation: "interactive" });
  env.setIdle(false);
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[0] * 2);
  await env.controller.tick();
  env.setIdle(true);
  await env.controller.tick();
  assert.deepEqual(env.sends, []);
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[0]);
  await env.controller.tick();
  assert.deepEqual(env.sends, ["intimacy_continue"]);
});

test("a quiet continuation ends the chain", async () => {
  const env = harness();
  env.complete("user-op", { presentation: "interactive" });
  await env.controller.tick();
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[0]);
  await env.controller.tick();
  env.complete(env.lastOperation(), { segments: [] });
  await env.controller.tick();
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[1]);
  await env.controller.tick();
  assert.deepEqual(env.sends, ["intimacy_continue"]);
});

test("user activity cancels the chain and the in-flight proposal once", async () => {
  const env = harness();
  env.complete("user-op", { presentation: "interactive" });
  await env.controller.tick();
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[0]);
  await env.controller.tick();
  const inFlight = env.lastOperation();

  env.controller.noteActivity();
  env.controller.noteActivity();
  assert.deepEqual(env.cancels, [inFlight]);
  env.complete(inFlight, { type: "chat.cancelled" });

  env.advance(INTIMACY_CONTINUE_DELAYS_MS[1]);
  await env.controller.tick();
  assert.deepEqual(env.sends, ["intimacy_continue"]);
  env.advance(RELATIONSHIP_PROPOSAL_INTERVAL_MS - INTIMACY_CONTINUE_DELAYS_MS[1]);
  await env.controller.tick();
  assert.deepEqual(env.sends, ["intimacy_continue", "relationship_initiative"]);
});

test("other silent turns never start a continuation chain", async () => {
  const env = harness();
  env.complete("screen-op", { presentation: "silent" });
  await env.controller.tick();
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[0]);
  await env.controller.tick();
  assert.deepEqual(env.sends, []);
});

test("a transient dispatch failure retries the continuation shortly after", async () => {
  const env = harness();
  env.sendResults.push(new Error("CHAT_INTERACTION_ACTIVE"));
  env.complete("user-op", { presentation: "interactive" });
  await env.controller.tick();
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[0]);
  await env.controller.tick();
  assert.deepEqual(env.sends, ["intimacy_continue"]);
  env.advance(INITIATIVE_RETRY_MS);
  await env.controller.tick();
  assert.deepEqual(env.sends, ["intimacy_continue", "intimacy_continue"]);
});

test("a terminal that arrives before the send resolves still settles the proposal", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const controller = createInitiativeController({
    send: async () => { await gate; return { operationId: "early" }; },
    cancel: async () => true,
    isIdle: () => true,
    now: () => RELATIONSHIP_PROPOSAL_INTERVAL_MS,
    setInterval: () => 1,
    clearInterval: () => {},
  });
  const dispatch = controller.tick();
  controller.handleChatEvent({ type: "chat.completed", operationId: "early", presentation: "silent", reply: { segments: [] } });
  release();
  await dispatch;
  assert.equal(controller.isPending(), false);
});

test("a generation change drops the chain and any pending proposal", async () => {
  const env = harness();
  env.complete("user-op", { presentation: "interactive" });
  await env.controller.tick();
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[0]);
  await env.controller.tick();
  env.controller.generationChanged();
  assert.equal(env.controller.isPending(), false);
  env.advance(INTIMACY_CONTINUE_DELAYS_MS[1]);
  await env.controller.tick();
  assert.deepEqual(env.sends, ["intimacy_continue"]);
});
