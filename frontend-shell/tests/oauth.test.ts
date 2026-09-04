import { describe, expect, it, vi } from "vitest";

import {
  CognitoPkceClient,
  OAUTH_STORAGE_KEYS,
  OAuthError,
  type CognitoOAuthConfig,
} from "../src/oauth.js";

class MemoryStorage implements Storage {
  readonly #values = new Map<string, string>();

  public get length(): number {
    return this.#values.size;
  }

  public clear(): void {
    this.#values.clear();
  }

  public getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  public key(index: number): string | null {
    return [...this.#values.keys()][index] ?? null;
  }

  public removeItem(key: string): void {
    this.#values.delete(key);
  }

  public setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }
}

const CONFIG: CognitoOAuthConfig = {
  authorizationEndpoint: "https://login.example.test/oauth2/authorize",
  tokenEndpoint: "https://login.example.test/oauth2/token",
  logoutEndpoint: "https://login.example.test/logout",
  clientId: "public-spa-client",
  redirectUri: "https://app.example.test/callback",
  logoutUri: "https://app.example.test/",
  scope: "openid kirocrew.control/invoke",
};

function transaction(storage: Storage): {
  readonly state: string;
  readonly verifier: string;
} {
  const raw = storage.getItem(OAUTH_STORAGE_KEYS.transaction);
  if (raw === null) {
    throw new Error("Expected an OAuth transaction.");
  }
  return JSON.parse(raw) as { state: string; verifier: string };
}

function requestUrl(input: string | URL | Request): string {
  return input instanceof Request ? input.url : input.toString();
}

function formBody(value: BodyInit | null | undefined): string {
  if (value instanceof URLSearchParams || typeof value === "string") {
    return value.toString();
  }
  throw new TypeError("Expected a URL-encoded form body.");
}

describe("CognitoPkceClient", () => {
  it("creates an S256 authorization request without a client secret", async () => {
    const storage = new MemoryStorage();
    const client = new CognitoPkceClient(CONFIG, { storage });

    const url = await client.createAuthorizationUrl("/projects");
    const saved = transaction(storage);

    expect(url.origin).toBe("https://login.example.test");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toMatch(/^[\w-]{43}$/u);
    expect(url.searchParams.get("state")).toBe(saved.state);
    expect(saved.verifier).toMatch(/^[\w-]{43}$/u);
    expect(url.toString()).not.toContain("client_secret");
  });

  it("validates state and exchanges the code with its one-time verifier", async () => {
    const storage = new MemoryStorage();
    const calls: Array<{ readonly url: string; readonly init?: RequestInit }> =
      [];
    const fetchMock = vi.fn(
      (
        input: string | URL | Request,
        init?: RequestInit,
      ): Promise<Response> => {
        calls.push({
          url: requestUrl(input),
          ...(init === undefined ? {} : { init }),
        });
        return Promise.resolve(
          Response.json({
            access_token: "access-value",
            refresh_token: "refresh-value",
            expires_in: 300,
            token_type: "Bearer",
            scope: "kirocrew.control/invoke",
          }),
        );
      },
    );
    const client = new CognitoPkceClient(CONFIG, {
      storage,
      fetch: fetchMock as typeof fetch,
      now: (): number => 1_000,
    });
    await client.createAuthorizationUrl("/chat");
    const saved = transaction(storage);

    const result = await client.handleCallback(
      new URL(
        `https://app.example.test/callback?code=one-time-code&state=${saved.state}`,
      ),
    );

    expect(result.returnTo).toBe("/chat");
    expect(result.session.accessToken).toBe("access-value");
    expect(storage.getItem(OAUTH_STORAGE_KEYS.transaction)).toBeNull();
    expect(storage.getItem(OAUTH_STORAGE_KEYS.session)).not.toContain(
      "one-time-code",
    );
    const body = calls[0]?.init?.body;
    expect(body).toBeInstanceOf(URLSearchParams);
    expect(formBody(body)).toContain(`code_verifier=${saved.verifier}`);
    expect(formBody(body)).not.toContain("client_secret");
  });

  it("rejects a state mismatch before contacting the token endpoint", async () => {
    const storage = new MemoryStorage();
    const fetchMock = vi.fn<typeof fetch>();
    const client = new CognitoPkceClient(CONFIG, {
      storage,
      fetch: fetchMock,
    });
    await client.createAuthorizationUrl();

    await expect(
      client.handleCallback(
        new URL(
          "https://app.example.test/callback?code=code&state=attacker-state",
        ),
      ),
    ).rejects.toMatchObject({ code: "STATE_MISMATCH" });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(storage.getItem(OAUTH_STORAGE_KEYS.transaction)).toBeNull();
  });

  it("refreshes an expiring access token and preserves a rotated refresh token", async () => {
    const storage = new MemoryStorage();
    storage.setItem(
      OAUTH_STORAGE_KEYS.session,
      JSON.stringify({
        accessToken: "old-access",
        refreshToken: "old-refresh",
        expiresAt: 20_000,
        tokenType: "Bearer",
        scope: "kirocrew.control/invoke",
      }),
    );
    const fetchMock = vi.fn(
      (
        _input: string | URL | Request,
        init?: RequestInit,
      ): Promise<Response> => {
        expect(formBody(init?.body)).toContain("grant_type=refresh_token");
        expect(formBody(init?.body)).toContain("refresh_token=old-refresh");
        return Promise.resolve(
          Response.json({
            access_token: "new-access",
            refresh_token: "new-refresh",
            expires_in: 600,
            token_type: "Bearer",
          }),
        );
      },
    );
    const client = new CognitoPkceClient(CONFIG, {
      storage,
      fetch: fetchMock as typeof fetch,
      now: (): number => 10_000,
    });

    await expect(client.accessToken()).resolves.toBe("new-access");
    expect(client.session()?.refreshToken).toBe("new-refresh");
  });

  it("clears browser-held auth state and constructs Cognito logout only", () => {
    const storage = new MemoryStorage();
    storage.setItem(OAUTH_STORAGE_KEYS.session, "sensitive-session");
    storage.setItem(OAUTH_STORAGE_KEYS.transaction, "sensitive-transaction");
    const client = new CognitoPkceClient(CONFIG, { storage });

    const logout = client.logoutUrl();

    expect(storage.length).toBe(0);
    expect(logout.origin).toBe("https://login.example.test");
    expect(logout.searchParams.get("logout_uri")).toBe(CONFIG.logoutUri);
    expect(logout.pathname).not.toContain("sandbox");
  });

  it("rejects non-HTTPS or cross-origin Cognito endpoint configuration", () => {
    expect(
      () =>
        new CognitoPkceClient(
          {
            ...CONFIG,
            tokenEndpoint: "https://other.example.test/oauth2/token",
          },
          { storage: new MemoryStorage() },
        ),
    ).toThrowError(OAuthError);
    expect(
      () =>
        new CognitoPkceClient(
          { ...CONFIG, authorizationEndpoint: "http://login.example.test" },
          { storage: new MemoryStorage() },
        ),
    ).toThrowError(OAuthError);
  });
});
