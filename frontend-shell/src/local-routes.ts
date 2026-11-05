/**
 * Two upstream features hand the browser a same-origin URL to GET directly,
 * which never reaches the gateway on this deployment: the browser talks to the
 * sandbox only through AgentCore invocations, so CloudFront's SPA fallback
 * answers a plain GET with index.html.
 *
 * - Inline widgets and artifact previews POST their HTML to `/api/sandbox-doc`
 *   and load the returned `/sandbox-doc/<id>/<token>` URL in an iframe. The
 *   frame showed "Can't open this page".
 * - `file_send` downloads are `<a href="/api/outbox/<name>">` navigations. The
 *   saved file was the 48 KB dashboard shell instead of the real bytes.
 *
 * The widget HTML is already in the browser (the stash is a mint for bytes the
 * caller holds), so the stash is answered here with a `data:` URL, never sent.
 * A `data:` document always gets an opaque origin, which keeps what the
 * gateway's `Content-Security-Policy: sandbox` header guaranteed: model HTML
 * never runs as the dashboard's origin, whatever the embedding frame grants.
 * Downloads are fetched through the bridged `fetch` (the invocation channel)
 * and saved from a Blob, and a failure is reported instead of saving a page.
 */

const SANDBOX_DOC_PATH = "/api/sandbox-doc";
const OUTBOX_PREFIX = "/api/outbox/";
/**
 * Chromium refuses URLs over 2 MB, so a larger document cannot ride a `data:`
 * URL. Upstream's own stash allows 8 MB; past 2 MB this answers its
 * `too_large` code, which the frame reports instead of blank-rendering.
 */
export const MAX_SANDBOX_DOC_BYTES = 1_500_000;

function jsonResponse(status: number, body: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  const method =
    init?.method ?? (input instanceof Request ? input.method : "GET");
  return method.toUpperCase();
}

function requestPath(
  input: RequestInfo | URL,
  origin: string,
): string | undefined {
  const raw = input instanceof Request ? input.url : input.toString();
  try {
    const url = new URL(raw, origin);
    return url.origin === origin ? url.pathname : undefined;
  } catch {
    return undefined;
  }
}

/** Whether this request is the sandbox-doc stash, answered locally. */
export function isSandboxDocStash(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  origin: string,
): boolean {
  return (
    requestMethod(input, init) === "POST" &&
    requestPath(input, origin) === SANDBOX_DOC_PATH
  );
}

/** Answer `POST /api/sandbox-doc` with a `data:` URL, in upstream's shape. */
export async function stashSandboxDoc(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
): Promise<Response> {
  let text: string;
  try {
    text =
      typeof init?.body === "string"
        ? init.body
        : input instanceof Request
          ? await input.text()
          : await new Response(init?.body ?? null).text();
  } catch {
    return jsonResponse(400, { error: "invalid json", code: "bad_body" });
  }
  let html: unknown;
  try {
    const parsed: unknown = JSON.parse(text);
    html =
      typeof parsed === "object" && parsed !== null
        ? (parsed as { html?: unknown }).html
        : undefined;
  } catch {
    return jsonResponse(400, { error: "invalid json", code: "bad_body" });
  }
  if (typeof html !== "string" || html.length === 0) {
    return jsonResponse(400, { error: "html required", code: "bad_html" });
  }
  const bytes = new TextEncoder().encode(html);
  if (bytes.length > MAX_SANDBOX_DOC_BYTES) {
    return jsonResponse(413, {
      error: "document too large",
      code: "too_large",
    });
  }
  return jsonResponse(200, {
    url: `data:text/html;charset=utf-8;base64,${bytesToBase64(bytes)}`,
  });
}

export interface OutboxDownloadOptions {
  readonly fetch: typeof fetch;
  readonly origin: string;
  readonly notify: (message: string) => void;
}

function fileNameFor(anchor: HTMLAnchorElement, path: string): string {
  const declared = anchor.getAttribute("download");
  if (declared !== null && declared.trim() !== "") {
    return declared.trim();
  }
  const last = path.slice(path.lastIndexOf("/") + 1);
  try {
    return decodeURIComponent(last) || "download";
  } catch {
    return last || "download";
  }
}

/** Fetch an outbox file through the channel and save it as the real bytes. */
export async function downloadOutboxFile(
  doc: Document,
  href: string,
  name: string,
  options: OutboxDownloadOptions,
): Promise<void> {
  let response: Response;
  try {
    response = await options.fetch(href, { method: "GET" });
  } catch {
    options.notify(
      `Could not download ${name}: the sandbox did not answer. Try again.`,
    );
    return;
  }
  if (!response.ok) {
    options.notify(
      `Could not download ${name} (HTTP ${String(response.status)}).`,
    );
    return;
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = doc.createElement("a");
  link.href = objectUrl;
  link.download = name;
  link.rel = "noopener";
  link.style.display = "none";
  doc.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => {
    URL.revokeObjectURL(objectUrl);
  }, 60_000);
}

/** Intercept clicks on `/api/outbox/` links so they go through the channel. */
export function installOutboxDownloads(
  doc: Document,
  options: OutboxDownloadOptions,
): () => void {
  const onClick = (event: MouseEvent): void => {
    if (event.defaultPrevented || event.button !== 0) {
      return;
    }
    const target = event.target;
    const anchor =
      target instanceof Element
        ? target.closest<HTMLAnchorElement>("a[href]")
        : null;
    if (anchor === null) {
      return;
    }
    const path = requestPath(anchor.href, options.origin);
    if (path === undefined || !path.startsWith(OUTBOX_PREFIX)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    void downloadOutboxFile(
      doc,
      anchor.href,
      fileNameFor(anchor, path),
      options,
    );
  };
  doc.addEventListener("click", onClick, true);
  return (): void => {
    doc.removeEventListener("click", onClick, true);
  };
}
