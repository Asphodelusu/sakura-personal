const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createMemorySearchController,
} = require("../../tools/settings-tauri/frontend/memory_search_controller.js");

function fakeClock() {
  let nextId = 1;
  let now = 0;
  const timers = new Map();
  return {
    setTimer(callback, delay) {
      const id = nextId++;
      timers.set(id, { callback, at: now + delay });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    advance(milliseconds) {
      now += milliseconds;
      const due = [...timers.entries()]
        .filter(([, timer]) => timer.at <= now)
        .sort((left, right) => left[1].at - right[1].at);
      for (const [id, timer] of due) {
        timers.delete(id);
        timer.callback();
      }
    },
  };
}

test("ordinary typing waits for a pause and coalesces into one search", () => {
  const clock = fakeClock();
  let searches = 0;
  const controller = createMemorySearchController({
    delayMs: 450,
    onSearch: () => {
      searches += 1;
    },
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  controller.onInput({ isComposing: false });
  clock.advance(300);
  controller.onInput({ isComposing: false });
  clock.advance(449);
  assert.equal(searches, 0);
  clock.advance(1);
  assert.equal(searches, 1);
});

test("IME composition searches once only after committed text settles", () => {
  const clock = fakeClock();
  let searches = 0;
  const controller = createMemorySearchController({
    delayMs: 450,
    onSearch: () => {
      searches += 1;
    },
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  controller.onCompositionStart();
  controller.onInput({ isComposing: true });
  clock.advance(1000);
  assert.equal(searches, 0);

  controller.onCompositionEnd();
  clock.advance(449);
  assert.equal(searches, 0);
  clock.advance(1);
  assert.equal(searches, 1);
});

test("only the latest request generation remains current", () => {
  const controller = createMemorySearchController({ onSearch: () => {} });

  const older = controller.beginRequest();
  const newer = controller.beginRequest();

  assert.equal(controller.isCurrentRequest(older), false);
  assert.equal(controller.isCurrentRequest(newer), true);
});
