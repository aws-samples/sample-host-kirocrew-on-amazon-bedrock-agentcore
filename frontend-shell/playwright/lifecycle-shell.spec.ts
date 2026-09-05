import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const SHELL_PATH = fileURLToPath(
  new URL("../dist/src/shell.js", import.meta.url),
);
const LIFECYCLE_PATH = fileURLToPath(
  new URL("../dist/src/lifecycle.js", import.meta.url),
);

interface LifecycleFixture {
  readonly view: string;
  readonly activeRequestAccepted: boolean;
  readonly startedAt?: number;
  readonly details?: {
    readonly events: readonly {
      readonly at: string;
      readonly state: string;
      readonly stateVersion: number;
    }[];
    readonly persistedPaths: readonly string[];
  };
  readonly sandbox?: {
    readonly sandboxId: string;
    readonly state: string;
    readonly stateVersion: number;
    readonly lastCheckpointAt: string | null;
    readonly lastRestore: string | null;
    readonly updatedAt: string;
  };
  readonly checkpointFreshness?: string;
  readonly restoreOutcome?: string;
  readonly error?: {
    readonly code: string;
    readonly message: string;
    readonly retryable: boolean;
  };
  readonly kiroAuth?: { readonly state: string };
  readonly deviceFlow?: {
    readonly verificationUri: string;
    readonly userCode: string;
    readonly expiresAt: string;
    readonly status: string;
  };
}

async function installShell(page: Page): Promise<void> {
  const html = `<!doctype html>
    <html>
      <head>
        <style>
          :root {
            --bg: #0d1117; --text: #f0f3f6; --text-strong: #ffffff;
            --card: #161b22; --card-fg: #f0f3f6; --border: #3d444d;
            --border-strong: #59636e; --muted: #aeb8c4; --accent: #58a6ff;
            --accent-hover: #79b8ff; --accent-subtle: #172b43;
            --ok: #3fb950; --ok-subtle: #17351f; --warn: #d29922;
            --warn-subtle: #3b2d13; --danger: #f85149;
            --danger-subtle: #3d1b1d; --bg-elevated: #21262d;
            --bg-hover: #30363d;
          }
          html, body, #root { margin: 0; min-height: 100%; }
        </style>
      </head>
      <body><div id="root"><button id="upstream-action">Upstream action</button></div></body>
    </html>`;
  await page.route("https://app.example.test/**", async (route) => {
    await route.fulfill({ contentType: "text/html", body: html });
  });
  await page.goto("https://app.example.test/");
  const [shellSource, lifecycleSource] = await Promise.all([
    readFile(SHELL_PATH, "utf8"),
    readFile(LIFECYCLE_PATH, "utf8"),
  ]);
  const lifecycleUrl = `data:text/javascript;base64,${Buffer.from(lifecycleSource).toString("base64")}`;
  const linkedShell = shellSource.replace(
    '"./lifecycle.js"',
    JSON.stringify(lifecycleUrl),
  );
  const shellUrl = `data:text/javascript;base64,${Buffer.from(linkedShell).toString("base64")}`;
  await page.evaluate(async (moduleUrl) => {
    const module = (await import(moduleUrl)) as {
      mountBrowserShell: (
        root: HTMLElement,
        actions: Record<string, (argument?: unknown) => void>,
      ) => { render: (model: unknown) => void };
    };
    const root = document.querySelector<HTMLElement>("#root");
    if (root === null) {
      throw new Error("Missing fixture root");
    }
    const calls = {
      signIn: 0,
      register: 0,
      forgotPassword: [] as unknown[],
      resetPassword: [] as unknown[],
      confirmEmail: [] as unknown[],
      resendCode: [] as unknown[],
      start: 0,
      stop: 0,
      retry: 0,
      logout: 0,
      kiroCheck: 0,
      kiroLogout: 0,
      kiroLogins: [] as unknown[],
    };
    const handle = module.mountBrowserShell(root, {
      signIn: (): void => {
        calls.signIn += 1;
      },
      register: (): void => {
        calls.register += 1;
      },
      forgotPassword: (email: unknown): void => {
        calls.forgotPassword.push(email);
      },
      resetPassword: (request: unknown): void => {
        calls.resetPassword.push(request);
      },
      confirmEmail: (request: unknown): void => {
        calls.confirmEmail.push(request);
      },
      resendCode: (credentials: unknown): void => {
        calls.resendCode.push(credentials);
      },
      start: (): void => {
        calls.start += 1;
      },
      stop: (): void => {
        calls.stop += 1;
      },
      retry: (): void => {
        calls.retry += 1;
      },
      logout: (): void => {
        calls.logout += 1;
      },
      kiroCheck: (): void => {
        calls.kiroCheck += 1;
      },
      kiroLogin: (options: unknown): void => {
        calls.kiroLogins.push(options ?? null);
      },
      kiroLogout: (): void => {
        calls.kiroLogout += 1;
      },
    });
    Object.assign(globalThis, { __task12Calls: calls, __task12Shell: handle });
  }, shellUrl);
}

async function render(page: Page, model: LifecycleFixture): Promise<void> {
  await page.evaluate((value) => {
    const fixture = globalThis as typeof globalThis & {
      __task12Shell: { render: (next: unknown) => void };
    };
    fixture.__task12Shell.render(value);
  }, model);
}

async function ensureExpanded(page: Page): Promise<void> {
  const pill = page.locator(".kcac-pill");
  if (await pill.isVisible()) {
    await pill.click();
  }
}

const STATES = [
  ["signed-out", "Sign in to KiroCrew"],
  ["stopped", "Sandbox stopped"],
  ["starting", "Starting sandbox"],
  ["restoring", "Restoring workspace"],
  ["ready", "Sandbox ready"],
  ["active-response", "KiroCrew is responding"],
  ["stopping", "Saving and stopping"],
  ["reconnecting", "Reconnecting"],
  ["read-only", "Workspace is read-only"],
  ["terminal-error", "Sandbox needs attention"],
] as const;

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await installShell(page);
});

test("renders every authoritative lifecycle view in the persistent rail", async ({
  page,
}) => {
  for (const [view, label] of STATES) {
    await render(page, {
      view,
      activeRequestAccepted: view === "active-response",
      ...(view === "terminal-error"
        ? {
            error: {
              code: "PERSISTENCE_RESTORE_FAILED",
              message: "The latest checkpoint could not be restored.",
              retryable: false,
            },
          }
        : {}),
    });
    await ensureExpanded(page);
    await expect(page.getByRole("heading", { name: label })).toBeVisible();
    await expect(page.getByRole("status")).toContainText(label);
    await expect(page.locator(".kcac-rail")).toHaveAttribute(
      "aria-label",
      "Sandbox status and controls",
    );
  }
});

test("exposes checkpoint freshness, restore outcome, and Device Flow safely", async ({
  page,
}) => {
  await render(page, {
    view: "ready",
    activeRequestAccepted: false,
    checkpointFreshness: "Saved 2 minutes ago",
    restoreOutcome: "Restored generation 42",
    deviceFlow: {
      verificationUri: "https://device.example.test/verify",
      userCode: "ABCD-EFGH",
      expiresAt: "2026-01-01T00:10:00.000Z",
      status: "required",
    },
  });

  await expect(page.getByText("Saved 2 minutes ago")).toBeVisible();
  await expect(page.getByText("Restored generation 42")).toBeVisible();
  await expect(page.getByText("ABCD-EFGH")).toBeVisible();
  const link = page.getByRole("link", { name: /Open Kiro verification page/u });
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(link).toHaveAttribute(
    "href",
    "https://device.example.test/verify",
  );
  // The raw address is shown as selectable text for manual copy.
  await expect(page.locator(".kcac-device-url")).toHaveText(
    "https://device.example.test/verify",
  );
});

test("supports keyboard-only controls and a visible focus indicator", async ({
  page,
}) => {
  await render(page, { view: "stopped", activeRequestAccepted: false });
  await page.keyboard.press("Tab");
  const start = page.getByRole("button", { name: "Start sandbox" });
  await expect(start).toBeFocused();
  const focus = await start.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      style: style.outlineStyle,
      width: Number.parseFloat(style.outlineWidth),
    };
  });
  expect(focus.style).not.toBe("none");
  expect(focus.width).toBeGreaterThanOrEqual(3);
  await page.keyboard.press("Enter");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            globalThis as typeof globalThis & {
              __task12Calls: { start: number };
            }
          ).__task12Calls.start,
      ),
    )
    .toBe(1);
});

test("sign-out disconnect action does not invoke sandbox stop or delete", async ({
  page,
}) => {
  await render(page, { view: "ready", activeRequestAccepted: false });
  await ensureExpanded(page);
  await page
    .getByRole("button", {
      name: "Sign out of this browser without stopping or deleting the sandbox",
    })
    .click();

  const calls = await page.evaluate(
    () =>
      (
        globalThis as typeof globalThis & {
          __task12Calls: { logout: number; stop: number };
        }
      ).__task12Calls,
  );
  expect(calls.logout).toBe(1);
  expect(calls.stop).toBe(0);
});

test("passes automated landmark, label, contrast, and secret-storage checks", async ({
  page,
}) => {
  await render(page, { view: "ready", activeRequestAccepted: false });
  await ensureExpanded(page);
  await expect(page.getByRole("banner")).toHaveCount(1);
  await expect(
    page.getByRole("main", { name: "KiroCrew application" }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("navigation", { name: "Sandbox actions" }),
  ).toHaveCount(1);
  const unnamed = await page
    .locator("button")
    .evaluateAll(
      (buttons) =>
        buttons.filter(
          (button) =>
            !button.textContent?.trim() &&
            !button.getAttribute("aria-label")?.trim(),
        ).length,
    );
  expect(unnamed).toBe(0);

  const contrast = await page.locator(".kcac-title").evaluate((element) => {
    const parse = (value: string): number[] =>
      value
        .match(/[\d.]+/gu)
        ?.slice(0, 3)
        .map(Number) ?? [];
    const luminance = (rgb: number[]): number => {
      const channels = rgb.map((channel) => {
        const normalized = channel / 255;
        return normalized <= 0.03928
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return (
        (channels[0] ?? 0) * 0.2126 +
        (channels[1] ?? 0) * 0.7152 +
        (channels[2] ?? 0) * 0.0722
      );
    };
    const foreground = luminance(parse(getComputedStyle(element).color));
    const rail = element.closest<HTMLElement>(".kcac-rail");
    const background = luminance(
      parse(getComputedStyle(rail ?? element).backgroundColor),
    );
    const light = Math.max(foreground, background);
    const dark = Math.min(foreground, background);
    return (light + 0.05) / (dark + 0.05);
  });
  expect(contrast).toBeGreaterThanOrEqual(4.5);
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
});

test("keeps actions usable at a narrow WCAG reflow viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await render(page, {
    view: "reconnecting",
    activeRequestAccepted: true,
    error: {
      code: "TRANSPORT_FAILED",
      message: "Connection lost.",
      retryable: true,
    },
  });

  await expect(
    page.getByRole("button", { name: "Check connection" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Sign out of this browser/u }),
  ).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth >
      document.documentElement.clientWidth,
  );
  expect(overflow).toBe(false);
});

test("collapses to a draggable status pill when the sandbox is calm", async ({
  page,
}) => {
  await render(page, { view: "ready", activeRequestAccepted: false });

  const float = page.locator(".kcac-float");
  const pill = page.locator(".kcac-pill");
  await expect(float).toHaveAttribute("data-mode", "pill");
  await expect(pill).toBeVisible();
  await expect(page.locator(".kcac-rail")).toBeHidden();
  await expect(pill).toHaveAttribute("title", "Sandbox ready");
  // The ball stays compact — no emphasis stretching in pill mode.
  const pillBox = await float.boundingBox();
  if (pillBox === null || pillBox.width > 160) {
    throw new Error(`Pill is not compact: ${JSON.stringify(pillBox)}`);
  }
  await expect(pill).toContainText("Sandbox");

  // Click expands the panel; Minimize collapses it again.
  await pill.click();
  await expect(float).toHaveAttribute("data-mode", "panel");
  await page.getByRole("button", { name: "Minimize the status panel" }).click();
  await expect(float).toHaveAttribute("data-mode", "pill");

  // A new attention state pops the panel open, and Minimize stays
  // available so the user can always shrink it back to the pill.
  await render(page, { view: "reconnecting", activeRequestAccepted: false });
  await expect(float).toHaveAttribute("data-mode", "panel");
  await page.getByRole("button", { name: "Minimize the status panel" }).click();
  await expect(float).toHaveAttribute("data-mode", "pill");
  // The same attention state stays minimized; a NEW one pops it open again.
  await render(page, { view: "reconnecting", activeRequestAccepted: false });
  await expect(float).toHaveAttribute("data-mode", "pill");
  await render(page, {
    view: "terminal-error",
    activeRequestAccepted: false,
    error: {
      code: "PERSISTENCE_RESTORE_FAILED",
      message: "The latest checkpoint could not be restored.",
      retryable: false,
    },
  });
  await expect(float).toHaveAttribute("data-mode", "panel");

  // A pending device flow also forces the panel open.
  await render(page, {
    view: "ready",
    activeRequestAccepted: false,
    deviceFlow: {
      verificationUri: "https://device.example.test/verify",
      userCode: "ABCD-EFGH",
      expiresAt: "2026-01-01T00:10:00.000Z",
      status: "required",
    },
  });
  await expect(float).toHaveAttribute("data-mode", "panel");
  await render(page, { view: "ready", activeRequestAccepted: false });
  await expect(float).toHaveAttribute("data-mode", "pill");

  // The pill is draggable with the pointer and stays inside the viewport.
  const before = await float.boundingBox();
  if (before === null) {
    throw new Error("Missing pill bounding box");
  }
  await page.mouse.move(
    before.x + before.width / 2,
    before.y + before.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(before.x - 160, before.y + 220, { steps: 8 });
  await page.mouse.up();
  const after = await float.boundingBox();
  if (after === null) {
    throw new Error("Missing pill bounding box after drag");
  }
  if (Math.abs(after.x - before.x) < 60 || Math.abs(after.y - before.y) < 60) {
    throw new Error(
      `Drag did not move the pill: ${JSON.stringify({ before, after })}`,
    );
  }
  // A drag must not toggle the panel open.
  await expect(float).toHaveAttribute("data-mode", "pill");
});

test("manages the Kiro account from the status panel", async ({ page }) => {
  // Not signed in: section shows sign-in choices and forces the panel open.
  await render(page, {
    view: "ready",
    activeRequestAccepted: false,
    kiroAuth: { state: "required" },
  });
  const float = page.locator(".kcac-float");
  // A connected sandbox minimizes automatically even before Kiro sign-in;
  // the panel is one pill-click away.
  await expect(float).toHaveAttribute("data-mode", "pill");
  await page.locator(".kcac-pill").click();
  await expect(float).toHaveAttribute("data-mode", "panel");
  await expect(float).toHaveAttribute("data-emphasis", "true");
  await expect(page.locator(".kcac-kiro-state")).toHaveText("Not signed in");
  await expect(
    page.getByRole("button", { name: "Sign out of Kiro" }),
  ).toBeHidden();

  // SSO requires an https start URL before the action fires.
  const ssoButton = page.getByRole("button", { name: "Sign in with SSO" });
  await ssoButton.click();
  await expect(page.getByLabel("Identity Center start URL")).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  await page
    .getByLabel("Identity Center start URL")
    .fill("https://example.awsapps.com/start");
  await page.getByLabel("Identity Center region").fill("us-east-1");
  await ssoButton.click();
  await page.getByRole("button", { name: "Sign in with Builder ID" }).click();
  await page.getByRole("button", { name: "Check status" }).click();
  const calls = await page.evaluate(
    () =>
      (
        globalThis as typeof globalThis & {
          __task12Calls: { kiroCheck: number; kiroLogins: unknown[] };
        }
      ).__task12Calls,
  );
  expect(calls.kiroCheck).toBe(1);
  expect(calls.kiroLogins).toEqual([
    {
      method: "sso",
      startUrl: "https://example.awsapps.com/start",
      region: "us-east-1",
    },
    null,
  ]);

  // The SSO settings persist for the next visit.
  const stored = await page.evaluate(() => ({
    url: localStorage.getItem("kirocrew.kiro.sso.startUrl"),
    region: localStorage.getItem("kirocrew.kiro.sso.region"),
  }));
  expect(stored).toEqual({
    url: "https://example.awsapps.com/start",
    region: "us-east-1",
  });

  // Signed in: the panel relaxes into the pill and offers sign-out.
  await render(page, {
    view: "ready",
    activeRequestAccepted: false,
    kiroAuth: { state: "authenticated" },
  });
  await expect(float).toHaveAttribute("data-mode", "pill");
  // The pill must fully hide the Kiro section (regression: giant rounded blob).
  await expect(page.locator(".kcac-kiro")).toBeHidden();

  // Failures pop the panel open so the hint is seen.
  await render(page, {
    view: "ready",
    activeRequestAccepted: false,
    kiroAuth: { state: "failed" },
  });
  await expect(float).toHaveAttribute("data-mode", "panel");
  await expect(page.locator(".kcac-kiro-hint")).toContainText(
    "did not complete",
  );

  await render(page, {
    view: "ready",
    activeRequestAccepted: false,
    kiroAuth: { state: "authenticated" },
  });
  await expect(float).toHaveAttribute("data-mode", "pill");
  await page.locator(".kcac-pill").click();
  await expect(page.locator(".kcac-kiro-state")).toHaveText("Signed in");
  await expect(
    page.getByRole("button", { name: "Sign in with SSO" }),
  ).toBeHidden();
  await page.getByRole("button", { name: "Sign out of Kiro" }).click();
  const logoutCalls = await page.evaluate(
    () =>
      (
        globalThis as typeof globalThis & {
          __task12Calls: { kiroLogout: number };
        }
      ).__task12Calls.kiroLogout,
  );
  expect(logoutCalls).toBe(1);
});

test("selecting panel text does not drag the panel", async ({ page }) => {
  await render(page, {
    view: "terminal-error",
    activeRequestAccepted: false,
    error: {
      code: "PERSISTENCE_RESTORE_FAILED",
      message: "The latest checkpoint could not be restored.",
      retryable: false,
    },
  });
  const float = page.locator(".kcac-float");
  const before = await float.boundingBox();
  if (before === null) {
    throw new Error("Missing panel bounding box");
  }
  // Drag across the error detail text as if selecting it.
  const detail = page.locator(".kcac-detail");
  const box = await detail.boundingBox();
  if (box === null) {
    throw new Error("Missing detail bounding box");
  }
  await page.mouse.move(box.x + 4, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 4, box.y + box.height / 2 + 60, {
    steps: 6,
  });
  await page.mouse.up();
  const after = await float.boundingBox();
  if (after === null) {
    throw new Error("Missing panel bounding box after selection");
  }
  if (Math.abs(after.x - before.x) > 1 || Math.abs(after.y - before.y) > 1) {
    throw new Error(
      `Panel moved during text selection: ${JSON.stringify({ before, after })}`,
    );
  }
  const selected = await page.evaluate(() => String(getSelection()));
  if (!selected.includes("checkpoint")) {
    throw new Error(`Text was not selectable: ${JSON.stringify(selected)}`);
  }
});

test("errors and reconnects never lock the running app out", async ({
  page,
}) => {
  const upstream = page.locator("#upstream-action");
  const content = page.locator(".kcac-content");

  // A terminal error reports the problem but the page stays usable: an
  // upstream agent may still be mid-turn behind the panel.
  await render(page, {
    view: "terminal-error",
    activeRequestAccepted: false,
    error: {
      code: "PERSISTENCE_RESTORE_FAILED",
      message: "The latest checkpoint could not be restored.",
      retryable: false,
    },
  });
  await expect(content).toHaveAttribute("data-blocked", "false");
  await upstream.click();
  await expect(upstream).toBeEnabled();

  await render(page, { view: "reconnecting", activeRequestAccepted: false });
  await expect(content).toHaveAttribute("data-blocked", "false");
  await upstream.click();

  await render(page, { view: "starting", activeRequestAccepted: false });
  await expect(content).toHaveAttribute("data-blocked", "false");

  // Without a sandbox behind it the content is still gated.
  await render(page, { view: "stopped", activeRequestAccepted: false });
  await expect(content).toHaveAttribute("data-blocked", "true");
});

test("startup journey stays on one steady card with visible progress", async ({
  page,
}) => {
  const title = page.locator(".kcac-title");
  const steps = page.locator(".kcac-step");

  await render(page, {
    view: "starting",
    activeRequestAccepted: false,
    startedAt: Date.now() - 5000,
  });
  await expect(title).toHaveText("Starting your sandbox");
  await expect(page.locator(".kcac-progress")).toBeVisible();
  await expect(page.locator(".kcac-elapsed")).toContainText("Elapsed");

  // Moving into restore advances the checklist without changing the title.
  await render(page, {
    view: "restoring",
    activeRequestAccepted: false,
    startedAt: Date.now() - 65000,
    sandbox: {
      sandboxId: "sbx_0123456789ABCDEFGHJKMNPQ",
      state: "RESTORING",
      stateVersion: 3,
      lastCheckpointAt: null,
      lastRestore: null,
      updatedAt: new Date().toISOString(),
    },
  });
  await expect(title).toHaveText("Starting your sandbox");
  await expect(steps.nth(0)).toHaveAttribute("data-state", "done");
  await expect(steps.nth(2)).toHaveAttribute("data-state", "active");

  // A dropped connection mid-startup must not flip the card to a warning.
  await render(page, {
    view: "reconnecting",
    activeRequestAccepted: false,
    startedAt: Date.now() - 90000,
    sandbox: {
      sandboxId: "sbx_0123456789ABCDEFGHJKMNPQ",
      state: "RESTORING",
      stateVersion: 3,
      lastCheckpointAt: null,
      lastRestore: null,
      updatedAt: new Date().toISOString(),
    },
  });
  await expect(title).toHaveText("Starting your sandbox");
  await expect(page.locator(".kcac-detail")).toContainText("re-establishing");

  // Once the journey window passes, ordinary presentation returns.
  await render(page, { view: "ready", activeRequestAccepted: false });
  await expect(title).toHaveText("Sandbox ready");
  await expect(page.locator(".kcac-progress")).toBeHidden();
});

test("signed-out view offers the credential form and submits sign-in", async ({
  page,
}) => {
  await render(page, { view: "signed-out", activeRequestAccepted: false });
  await ensureExpanded(page);
  const email = page.getByLabel("Email address");
  const password = page.getByLabel("Password", { exact: true });
  await expect(email).toBeVisible();
  // Empty submissions never leave the form.
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator(".kcac-auth-hint")).toContainText(
    "Enter your email",
  );
  await email.fill("dev@amazon.com");
  await password.fill("correct-horse");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const signIns = await page.evaluate(
    () =>
      (globalThis as typeof globalThis & { __task12Calls: { signIn: number } })
        .__task12Calls.signIn,
  );
  expect(signIns).toBe(1);
  // Other lifecycle views hide the form again.
  await render(page, { view: "stopped", activeRequestAccepted: false });
  await expect(email).toBeHidden();
});

test("registration asks for the emailed code and can resend it", async ({
  page,
}) => {
  await render(page, { view: "signed-out", activeRequestAccepted: false });
  await ensureExpanded(page);
  const registerPassword = ["correct", "horse", "14"].join("-");
  await page.getByLabel("Email address").fill("dev@amazon.com");
  await page.getByLabel("Password", { exact: true }).fill(registerPassword);
  await page.getByRole("button", { name: "Create account" }).click();
  // Success flips the form into the confirmation step.
  await expect(page.locator(".kcac-auth-hint")).toContainText(
    "emailed a code to dev@amazon.com",
  );
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeHidden();
  // A fresh code can be requested while unconfirmed.
  await page.getByRole("button", { name: "Resend code" }).click();
  await expect(page.locator(".kcac-auth-hint")).toContainText(
    "A new code is on its way",
  );
  // Confirming requires the code.
  await page.getByRole("button", { name: "Confirm email" }).click();
  await expect(page.locator(".kcac-auth-hint")).toContainText("Enter the code");
  await page.getByLabel("Confirmation code").fill("654321");
  await page.getByRole("button", { name: "Confirm email" }).click();
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
  const calls = await page.evaluate(
    () =>
      (
        globalThis as typeof globalThis & {
          __task12Calls: { confirmEmail: unknown[]; resendCode: unknown[] };
        }
      ).__task12Calls,
  );
  expect(calls.resendCode).toEqual([
    { email: "dev@amazon.com", password: registerPassword },
  ]);
  expect(calls.confirmEmail).toEqual([
    { email: "dev@amazon.com", password: registerPassword, code: "654321" },
  ]);
});

test("the eye toggle reveals and hides the password text", async ({ page }) => {
  await render(page, { view: "signed-out", activeRequestAccepted: false });
  await ensureExpanded(page);
  const password = page.getByLabel("Password", { exact: true });
  await password.fill("hunter2hunter2");
  await expect(password).toHaveAttribute("type", "password");
  const eye = page.getByRole("button", { name: "Show password" }).first();
  await eye.click();
  await expect(password).toHaveAttribute("type", "text");
  await expect(
    page.getByRole("button", { name: "Hide password" }).first(),
  ).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Hide password" }).first().click();
  await expect(password).toHaveAttribute("type", "password");
});

test("forgot password sends a code and resets through the form", async ({
  page,
}) => {
  await render(page, { view: "signed-out", activeRequestAccepted: false });
  await ensureExpanded(page);
  await page.getByRole("button", { name: "Forgot password?" }).click();
  // The sign-in controls yield to the reset controls.
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeHidden();
  const email = page.getByLabel("Email address");
  // Sending a code requires an email address.
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.locator(".kcac-auth-hint")).toContainText(
    "Enter your email",
  );
  await email.fill("dev@amazon.com");
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.locator(".kcac-auth-hint")).toContainText(
    "on its way to dev@amazon.com",
  );
  const newPassword = ["correct", "horse", "again"].join("-");
  await page.getByLabel("Verification code").fill("123456");
  await page.getByLabel("New password").fill(newPassword);
  await page.getByRole("button", { name: "Reset password" }).click();
  await expect(page.locator(".kcac-auth-hint")).toContainText(
    "Password updated",
  );
  // Success returns to the sign-in mode with the credentials cleared.
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
  const calls = await page.evaluate(
    () =>
      (
        globalThis as typeof globalThis & {
          __task12Calls: {
            forgotPassword: unknown[];
            resetPassword: unknown[];
          };
        }
      ).__task12Calls,
  );
  expect(calls.forgotPassword).toEqual(["dev@amazon.com"]);
  expect(calls.resetPassword).toEqual([
    {
      email: "dev@amazon.com",
      code: "123456",
      password: newPassword,
    },
  ]);
  // The way back to sign-in never requires a successful reset.
  await page.getByRole("button", { name: "Forgot password?" }).click();
  await page.getByRole("button", { name: "Back to sign in" }).click();
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
});

test("sandbox details show observed history and persisted paths", async ({
  page,
}) => {
  await render(page, { view: "signed-out", activeRequestAccepted: false });
  await ensureExpanded(page);
  // Signed out: no details section even when data exists.
  await render(page, {
    view: "signed-out",
    activeRequestAccepted: false,
    details: {
      events: [{ at: "2026-09-05T10:00:00Z", state: "READY", stateVersion: 3 }],
      persistedPaths: ["/mnt/workspace/projects"],
    },
  });
  await expect(
    page.getByRole("button", { name: "Sandbox details" }),
  ).toBeHidden();
  // Signed in with details: collapsed by default, expands on demand.
  await render(page, {
    view: "stopped",
    activeRequestAccepted: false,
    details: {
      events: [
        { at: "2026-09-05T10:00:00Z", state: "READY", stateVersion: 3 },
        { at: "2026-09-05T09:00:00Z", state: "STOPPED", stateVersion: 2 },
      ],
      persistedPaths: ["/mnt/workspace/projects", "/mnt/workspace/user"],
    },
  });
  const toggle = page.getByRole("button", { name: "Sandbox details" });
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const history = page.getByLabel("Sandbox state history");
  await expect(history.locator("li")).toHaveCount(2);
  await expect(history.locator("li").first()).toContainText("ready");
  const paths = page.getByLabel("Persisted paths");
  await expect(paths.locator("li")).toHaveCount(2);
  await expect(paths.locator("li").first()).toHaveText(
    "/mnt/workspace/projects",
  );
});
