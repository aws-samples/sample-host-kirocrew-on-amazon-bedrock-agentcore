/**
 * Page half of the app-asset bridge (see app-asset-worker.ts).
 *
 * The service worker posts `{type, path}` with a reply port; the page fetches
 * the path through the remote transport and transfers the bytes back. Only an
 * installed app's static `ui`/`art` paths are answered, so a worker message can
 * never turn the page into a proxy for anything else.
 */
import { APP_ASSET_MESSAGE, type AppAssetReply } from "./app-asset-worker.js";
import { appRouteKind } from "./remote-transport.js";

interface ReplyPort {
  postMessage(message: AppAssetReply, transfer?: Transferable[]): void;
}

export interface ServiceWorkerMessageEventLike {
  readonly data: unknown;
  readonly ports: readonly ReplyPort[];
}

export interface ServiceWorkerContainerLike {
  addEventListener(
    type: "message",
    listener: (event: ServiceWorkerMessageEventLike) => void,
  ): void;
  removeEventListener(
    type: "message",
    listener: (event: ServiceWorkerMessageEventLike) => void,
  ): void;
}

export function installAppAssetBridge(
  container: ServiceWorkerContainerLike | undefined,
  fetchThroughSandbox: (url: string) => Promise<Response>,
  origin: string,
): () => void {
  if (container === undefined) {
    return (): void => undefined;
  }
  const listener = (event: ServiceWorkerMessageEventLike): void => {
    const message = event.data as { type?: unknown; path?: unknown } | null;
    const port = event.ports[0];
    if (
      message === null ||
      typeof message !== "object" ||
      message.type !== APP_ASSET_MESSAGE ||
      port === undefined
    ) {
      return;
    }
    const path = message.path;
    let url: URL | undefined;
    try {
      url = typeof path === "string" ? new URL(path, origin) : undefined;
    } catch {
      url = undefined;
    }
    const kind = url === undefined ? undefined : appRouteKind(url.pathname);
    if (
      url === undefined ||
      url.origin !== origin ||
      (kind !== "ui" && kind !== "art")
    ) {
      port.postMessage({ ok: false });
      return;
    }
    void fetchThroughSandbox(url.toString())
      .then(async (response) => {
        const body = await response.arrayBuffer();
        port.postMessage(
          {
            ok: true,
            status: response.status,
            contentType:
              response.headers.get("content-type") ??
              "application/octet-stream",
            body,
          },
          [body],
        );
      })
      .catch(() => {
        port.postMessage({ ok: false });
      });
  };
  container.addEventListener("message", listener);
  return (): void => {
    container.removeEventListener("message", listener);
  };
}
