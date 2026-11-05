import { describe, expect, it, vi } from "vitest";

import {
  downloadOutboxFile,
  installOutboxDownloads,
  isSandboxDocStash,
  MAX_SANDBOX_DOC_BYTES,
  stashSandboxDoc,
} from "../src/local-routes.js";

const ORIGIN = "https://d123.cloudfront.net";

function decodeDataUrl(url: string): string {
  const prefix = "data:text/html;charset=utf-8;base64,";
  expect(url.startsWith(prefix)).toBe(true);
  const binary = atob(url.slice(prefix.length));
  return new TextDecoder().decode(
    Uint8Array.from(binary, (c) => c.charCodeAt(0)),
  );
}

describe("sandbox-doc stash", () => {
  it("matches only a same-origin POST to /api/sandbox-doc", () => {
    expect(
      isSandboxDocStash("/api/sandbox-doc", { method: "POST" }, ORIGIN),
    ).toBe(true);
    expect(
      isSandboxDocStash(
        `${ORIGIN}/api/sandbox-doc`,
        { method: "post" },
        ORIGIN,
      ),
    ).toBe(true);
    expect(isSandboxDocStash("/api/sandbox-doc", undefined, ORIGIN)).toBe(
      false,
    );
    expect(
      isSandboxDocStash("/api/sandbox-doc/x", { method: "POST" }, ORIGIN),
    ).toBe(false);
    expect(
      isSandboxDocStash(
        "https://evil.example/api/sandbox-doc",
        { method: "POST" },
        ORIGIN,
      ),
    ).toBe(false);
    expect(
      isSandboxDocStash(
        new Request(`${ORIGIN}/api/sandbox-doc`, { method: "POST" }),
        undefined,
        ORIGIN,
      ),
    ).toBe(true);
  });

  it("answers with a data: URL carrying the exact HTML, including non-ASCII", async () => {
    const html = "<p>鸟群 ✓</p>";
    const response = await stashSandboxDoc("/api/sandbox-doc", {
      method: "POST",
      body: JSON.stringify({ html }),
    });
    expect(response.status).toBe(200);
    const body = (await response.json()) as { url: string };
    expect(decodeDataUrl(body.url)).toBe(html);
  });

  it("reads the body from a Request input", async () => {
    const request = new Request(`${ORIGIN}/api/sandbox-doc`, {
      method: "POST",
      body: JSON.stringify({ html: "<b>x</b>" }),
    });
    const body = (await (await stashSandboxDoc(request, undefined)).json()) as {
      url: string;
    };
    expect(decodeDataUrl(body.url)).toBe("<b>x</b>");
  });

  it.each([
    ["not json", "bad_body", 400],
    [JSON.stringify(["html"]), "bad_html", 400],
    [JSON.stringify({ html: "" }), "bad_html", 400],
    [JSON.stringify({ html: 3 }), "bad_html", 400],
    [
      JSON.stringify({ html: "a".repeat(MAX_SANDBOX_DOC_BYTES + 1) }),
      "too_large",
      413,
    ],
  ])("refuses %s with upstream's error code", async (body, code, status) => {
    const response = await stashSandboxDoc("/api/sandbox-doc", {
      method: "POST",
      body,
    });
    expect(response.status).toBe(status);
    expect(((await response.json()) as { code: string }).code).toBe(code);
  });
});

interface FakeLink {
  href: string;
  download: string;
  rel: string;
  style: { display: string };
  click: () => void;
  remove: () => void;
}

function fakeDocument(): {
  doc: Document;
  links: FakeLink[];
  fire: (event: object) => void;
} {
  const links: FakeLink[] = [];
  let listener: ((event: MouseEvent) => void) | undefined;
  const doc = {
    createElement: () => {
      const link: FakeLink = {
        href: "",
        download: "",
        rel: "",
        style: { display: "" },
        click: vi.fn(),
        remove: vi.fn(),
      };
      links.push(link);
      return link;
    },
    body: { appendChild: vi.fn() },
    addEventListener: (_: string, handler: (event: MouseEvent) => void) => {
      listener = handler;
    },
    removeEventListener: () => {
      listener = undefined;
    },
  } as unknown as Document;
  return {
    doc,
    links,
    fire: (event): void => {
      listener?.(event as unknown as MouseEvent);
    },
  };
}

describe("outbox downloads", () => {
  it("saves the fetched bytes under the file's own name", async () => {
    const { doc, links } = fakeDocument();
    const fetchFn = vi.fn(() =>
      Promise.resolve(new Response("real bytes", { status: 200 })),
    );
    const notify = vi.fn();
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    await downloadOutboxFile(
      doc,
      `${ORIGIN}/api/outbox/report_ab.md`,
      "report_ab.md",
      {
        fetch: fetchFn as unknown as typeof fetch,
        origin: ORIGIN,
        notify,
      },
    );
    expect(fetchFn).toHaveBeenCalledWith(`${ORIGIN}/api/outbox/report_ab.md`, {
      method: "GET",
    });
    const blob = create.mock.calls[0]?.[0] as Blob;
    expect(await blob.text()).toBe("real bytes");
    expect(links[0]?.download).toBe("report_ab.md");
    expect(links[0]?.click).toHaveBeenCalled();
    expect(notify).not.toHaveBeenCalled();
    create.mockRestore();
  });

  it("reports a failed fetch instead of saving anything", async () => {
    const { doc, links } = fakeDocument();
    const notify = vi.fn();
    await downloadOutboxFile(doc, "/api/outbox/a", "a", {
      fetch: (() =>
        Promise.resolve(new Response("", { status: 404 }))) as typeof fetch,
      origin: ORIGIN,
      notify,
    });
    await downloadOutboxFile(doc, "/api/outbox/a", "a", {
      fetch: (() => Promise.reject(new Error("down"))) as typeof fetch,
      origin: ORIGIN,
      notify,
    });
    expect(links).toHaveLength(0);
    expect(notify).toHaveBeenCalledTimes(2);
    expect(notify.mock.calls[0]?.[0]).toContain("HTTP 404");
  });

  it("intercepts only primary clicks on same-origin outbox links", async () => {
    const { doc, fire } = fakeDocument();
    const fetchFn = vi.fn(() => Promise.resolve(new Response("x")));
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    const uninstall = installOutboxDownloads(doc, {
      fetch: fetchFn as unknown as typeof fetch,
      origin: ORIGIN,
      notify: vi.fn(),
    });
    const anchor = (href: string, download: string | null = null): Element =>
      ({
        closest: () => ({ href, getAttribute: () => download }),
      }) as unknown as Element;
    const event = (
      target: Element,
      button = 0,
    ): {
      target: Element;
      button: number;
      defaultPrevented: boolean;
      preventDefault: ReturnType<typeof vi.fn>;
      stopPropagation: ReturnType<typeof vi.fn>;
      prevent: ReturnType<typeof vi.fn>;
    } => {
      const prevent = vi.fn();
      return {
        target,
        button,
        defaultPrevented: false,
        preventDefault: prevent,
        stopPropagation: vi.fn(),
        prevent,
      };
    };
    // Element instanceof checks need a real Element class in node.
    const ElementCtor = (globalThis as { Element?: unknown }).Element;
    if (ElementCtor === undefined) {
      (globalThis as { Element?: unknown }).Element = Object;
    }
    const hit = event(anchor(`${ORIGIN}/api/outbox/f%20one.html`));
    fire(hit);
    const other = event(anchor(`${ORIGIN}/api/sessions`));
    fire(other);
    const foreign = event(anchor("https://evil.example/api/outbox/x"));
    fire(foreign);
    const middle = event(anchor(`${ORIGIN}/api/outbox/x`), 1);
    fire(middle);
    expect(hit.prevent).toHaveBeenCalled();
    expect(other.prevent).not.toHaveBeenCalled();
    expect(foreign.prevent).not.toHaveBeenCalled();
    expect(middle.prevent).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(1));
    uninstall();
    if (ElementCtor === undefined) {
      delete (globalThis as { Element?: unknown }).Element;
    }
  });
});
