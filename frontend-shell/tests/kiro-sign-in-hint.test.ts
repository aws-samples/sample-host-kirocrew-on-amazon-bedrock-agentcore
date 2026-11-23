import { describe, expect, it, vi } from "vitest";

import {
  installKiroSignInHint,
  navigateInPage,
  SIGN_IN_PATH,
} from "../src/kiro-sign-in-hint.js";

class FakeElement {
  public id = "";
  public className = "";
  public type = "";
  public textContent: string | null = "";
  public readonly children: FakeElement[] = [];
  public readonly attributes = new Map<string, string>();
  public readonly listeners = new Map<string, () => void>();
  public parent: FakeElement | undefined;

  public constructor(public readonly tagName: string) {}

  public append(...nodes: FakeElement[]): void {
    for (const node of nodes) {
      this.appendChild(node);
    }
  }

  public appendChild(node: FakeElement): FakeElement {
    node.parent = this;
    this.children.push(node);
    return node;
  }

  public remove(): void {
    if (this.parent !== undefined) {
      this.parent.children.splice(this.parent.children.indexOf(this), 1);
      this.parent = undefined;
    }
  }

  public setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
  }

  public addEventListener(name: string, listener: () => void): void {
    this.listeners.set(name, listener);
  }

  public find(
    predicate: (element: FakeElement) => boolean,
  ): FakeElement | undefined {
    for (const child of this.children) {
      if (predicate(child)) {
        return child;
      }
      const found = child.find(predicate);
      if (found !== undefined) {
        return found;
      }
    }
    return undefined;
  }
}

function fakeDocument(): {
  doc: Document;
  head: FakeElement;
  body: FakeElement;
  notice: () => FakeElement | undefined;
} {
  const head = new FakeElement("head");
  const body = new FakeElement("body");
  const doc = {
    head,
    body,
    createElement: (tag: string) => new FakeElement(tag),
    getElementById: (id: string) =>
      head.find((element) => element.id === id) ?? null,
  } as unknown as Document;
  return {
    doc,
    head,
    body,
    notice: () => body.children[0],
  };
}

function sources(...answers: unknown[]): typeof fetch {
  let index = 0;
  return vi.fn(() => {
    const answer = answers[Math.min(index, answers.length - 1)];
    index += 1;
    if (answer instanceof Error) {
      return Promise.reject(answer);
    }
    if (typeof answer === "number") {
      return Promise.resolve(new Response("{}", { status: answer }));
    }
    return Promise.resolve(new Response(JSON.stringify(answer)));
  }) as unknown as typeof fetch;
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
};

function timers(): {
  setTimeout: (callback: () => void, delay: number) => unknown;
  clearTimeout: (handle: unknown) => void;
  run: () => void;
  pending: () => number;
} {
  const queue = new Map<number, () => void>();
  let next = 0;
  return {
    setTimeout: (callback): number => {
      next += 1;
      queue.set(next, callback);
      return next;
    },
    clearTimeout: (handle): void => {
      queue.delete(handle as number);
    },
    run: (): void => {
      const callbacks = [...queue.values()];
      queue.clear();
      for (const callback of callbacks) {
        callback();
      }
    },
    pending: (): number => queue.size,
  };
}

describe("Kiro sign-in hint", () => {
  it("shows a notice while nobody is signed in and removes it once someone is", async () => {
    const { doc, head, notice } = fakeDocument();
    const clock = timers();
    const navigate = vi.fn();
    const fetchFn = sources(
      { active: "none" },
      { active: "none" },
      { active: "kiro_crew" },
    );
    installKiroSignInHint({
      fetch: fetchFn,
      document: doc,
      navigate,
      ...clock,
    });
    await flush();

    const shown = notice();
    expect(shown?.id).toBe("kirocrew-agentcore-sign-in-hint");
    expect(shown?.attributes.get("role")).toBe("status");
    expect(head.children).toHaveLength(1);
    expect(fetchFn).toHaveBeenCalledWith("/api/kas-login/sources", {
      method: "GET",
    });

    // Still signed out: the same notice stays, styles are not added twice.
    clock.run();
    await flush();
    expect(notice()).toBe(shown);
    expect(head.children).toHaveLength(1);
    // The button opens the sign-in page and puts the notice away.
    shown
      ?.find((element) => element.className === "kcac-hint-go")
      ?.listeners.get("click")?.();
    expect(navigate).toHaveBeenCalledWith(SIGN_IN_PATH);
    expect(notice()).toBeUndefined();
    // Signed in: the notice goes and checking stops.
    clock.run();
    await flush();
    expect(notice()).toBeUndefined();
    expect(clock.pending()).toBe(0);
  });

  it("steps aside on the sign-in page and returns elsewhere while signed out", async () => {
    const { doc, notice } = fakeDocument();
    const clock = timers();
    let path = "/chat";
    installKiroSignInHint({
      fetch: sources({ active: "none" }),
      document: doc,
      navigate: (target) => {
        path = target;
      },
      currentPath: () => path,
      ...clock,
    });
    await flush();
    notice()
      ?.find((element) => element.className === "kcac-hint-go")
      ?.listeners.get("click")?.();
    expect(path).toBe(SIGN_IN_PATH);
    expect(notice()).toBeUndefined();
    clock.run();
    await flush();
    expect(notice()).toBeUndefined();
    path = "/chat";
    clock.run();
    await flush();
    expect(notice()).toBeDefined();
  });

  it("stays quiet for a signed-in, unknown or failing answer", async () => {
    for (const answer of [
      { active: "kiro_cli" },
      { active: "unknown" },
      500,
      new Error("offline"),
    ]) {
      const { doc, notice } = fakeDocument();
      const clock = timers();
      installKiroSignInHint({
        fetch: sources(answer),
        document: doc,
        navigate: vi.fn(),
        ...clock,
      });
      await flush();
      expect(notice()).toBeUndefined();
      expect(clock.pending()).toBe(0);
    }
  });

  it("stays dismissed and stops cleanly", async () => {
    const { doc, notice } = fakeDocument();
    const clock = timers();
    const uninstall = installKiroSignInHint({
      fetch: sources({ active: "none" }),
      document: doc,
      navigate: vi.fn(),
      ...clock,
      pollMs: 5,
    });
    await flush();
    const close = notice()?.find(
      (element) => element.className === "kcac-hint-close",
    );
    close?.listeners.get("click")?.();
    expect(notice()).toBeUndefined();
    clock.run();
    await flush();
    expect(notice()).toBeUndefined();
    expect(clock.pending()).toBe(0);

    const second = fakeDocument();
    const secondClock = timers();
    const stop = installKiroSignInHint({
      fetch: sources({ active: "none" }),
      document: second.doc,
      navigate: vi.fn(),
      ...secondClock,
    });
    await flush();
    expect(secondClock.pending()).toBe(1);
    stop();
    expect(second.notice()).toBeUndefined();
    expect(secondClock.pending()).toBe(0);
    uninstall();

    // A check that answers after uninstall shows nothing.
    let release: (value: Response) => void = () => undefined;
    const late = fakeDocument();
    const stopLate = installKiroSignInHint({
      fetch: () => new Promise<Response>((resolve) => (release = resolve)),
      document: late.doc,
      navigate: vi.fn(),
      ...timers(),
    });
    stopLate();
    release(new Response(JSON.stringify({ active: "none" })));
    await flush();
    expect(late.notice()).toBeUndefined();
  });

  it("navigates in page through the router's popstate", () => {
    vi.stubGlobal(
      "PopStateEvent",
      class extends Event {
        public constructor(type: string) {
          super(type);
        }
      },
    );
    const pushState = vi.fn();
    const dispatchEvent = vi.fn();
    const target = {
      location: { pathname: "/chat" },
      history: { pushState },
      dispatchEvent,
    } as unknown as Window;
    navigateInPage(target, SIGN_IN_PATH);
    expect(pushState).toHaveBeenCalledWith({}, "", SIGN_IN_PATH);
    expect(dispatchEvent).toHaveBeenCalledOnce();
    navigateInPage(
      { ...target, location: { pathname: SIGN_IN_PATH } } as unknown as Window,
      SIGN_IN_PATH,
    );
    expect(pushState).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });
});
