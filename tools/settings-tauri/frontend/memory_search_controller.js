(function exposeMemorySearchController(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.SakuraMemorySearch = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  function createMemorySearchController(options = {}) {
    const delayMs = Number(options.delayMs ?? 450);
    const onSearch = options.onSearch;
    const setTimer = options.setTimer || ((callback, delay) => window.setTimeout(callback, delay));
    const clearTimer = options.clearTimer || ((timerId) => window.clearTimeout(timerId));
    let timerId = null;
    let composing = false;
    let requestGeneration = 0;

    const cancelPending = () => {
      if (timerId !== null) {
        clearTimer(timerId);
        timerId = null;
      }
    };

    const invalidateRequests = () => {
      requestGeneration += 1;
    };

    const schedule = () => {
      cancelPending();
      timerId = setTimer(() => {
        timerId = null;
        onSearch();
      }, delayMs);
    };

    return {
      onInput(event = {}) {
        invalidateRequests();
        cancelPending();
        if (composing || event.isComposing) {
          return;
        }
        schedule();
      },
      onCompositionStart() {
        composing = true;
        invalidateRequests();
        cancelPending();
      },
      onCompositionEnd() {
        composing = false;
        invalidateRequests();
        schedule();
      },
      cancelPending,
      beginRequest() {
        requestGeneration += 1;
        return requestGeneration;
      },
      isCurrentRequest(generation) {
        return generation === requestGeneration;
      },
    };
  }

  return { createMemorySearchController };
});
