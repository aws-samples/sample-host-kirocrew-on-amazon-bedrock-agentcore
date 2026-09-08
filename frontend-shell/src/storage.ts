/**
 * Where a signed-in session is kept between visits.
 *
 * `sessionStorage` is scoped to one tab and the browser discards it when that
 * tab closes, so a session kept there survives a reload but not closing the
 * page. A session is meant to outlive the tab, so it belongs in `localStorage`.
 *
 * Reading either store can *throw* rather than answer -- a private window, a
 * cookie policy, or an embedder that blocks storage on the origin -- so the
 * durable one is probed behind a guard and `sessionStorage` stays the fallback.
 * Falling back leaves exactly the previous behaviour (a session that lasts as
 * long as the tab) instead of failing to construct the client at all.
 */
function reachable(pick: () => Storage): Storage | undefined {
  try {
    return pick();
  } catch {
    return undefined;
  }
}

export function durableStorage(): Storage {
  return reachable(() => globalThis.localStorage) ?? sessionStorage;
}
