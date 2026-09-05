import { beforeEach, describe, expect, it } from "vitest";

import {
  AUTH_STORAGE_KEYS,
  PasswordAuthClient,
  PasswordAuthError,
} from "../src/auth.js";

class MemoryStorage implements Storage {
  #values = new Map<string, string>();

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

interface RecordedRequest {
  readonly url: string;
  readonly body: unknown;
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function client(
  responses: Array<Response | Error>,
  now: () => number = () => 1_000,
): {
  readonly auth: PasswordAuthClient;
  readonly storage: MemoryStorage;
  readonly requests: RecordedRequest[];
} {
  const storage = new MemoryStorage();
  const requests: RecordedRequest[] = [];
  const fetchStub = ((
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    requests.push({
      url: input as string,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });
    const next = responses.shift();
    if (next === undefined) {
      return Promise.reject(new Error("Unexpected request."));
    }
    if (next instanceof Error) {
      return Promise.reject(next);
    }
    return Promise.resolve(next);
  }) as typeof fetch;
  const auth = new PasswordAuthClient(
    { basePath: "/auth/v1" },
    { storage, fetch: fetchStub, now },
  );
  return { auth, storage, requests };
}

const TOKENS = {
  accessToken: "access-token",
  idToken: "id-token",
  refreshToken: "refresh-token",
  expiresIn: 3600,
  tokenType: "Bearer",
};

describe("PasswordAuthClient", () => {
  let subject: ReturnType<typeof client>;

  beforeEach(() => {
    subject = client([]);
  });

  it("rejects non-same-origin base paths", () => {
    expect(
      () => new PasswordAuthClient({ basePath: "https://evil.example" }),
    ).toThrow(PasswordAuthError);
    expect(() => new PasswordAuthClient({ basePath: "//evil" })).toThrow(
      PasswordAuthError,
    );
  });

  it("signs in, stores the session, and serves the access token", async () => {
    subject = client([jsonResponse(200, TOKENS)]);
    const session = await subject.auth.signIn("dev@amazon.com", "secret");
    expect(session.accessToken).toBe("access-token");
    expect(subject.requests[0]).toEqual({
      url: "/auth/v1/login",
      body: { email: "dev@amazon.com", password: "secret" },
    });
    expect(subject.storage.getItem(AUTH_STORAGE_KEYS.session)).not.toBeNull();
    await expect(subject.auth.accessToken()).resolves.toBe("access-token");
  });

  it("maps server error codes onto stable client codes", async () => {
    subject = client([
      jsonResponse(403, { code: "DOMAIN_NOT_ALLOWED", message: "No." }),
      jsonResponse(401, { code: "SIGN_IN_FAILED", message: "Wrong." }),
      jsonResponse(409, { code: "USER_EXISTS", message: "Exists." }),
      jsonResponse(502, { code: "SIGN_IN_UNAVAILABLE", message: "Down." }),
      jsonResponse(500, {}),
      new Error("network down"),
    ]);
    const signIn = (): Promise<unknown> => subject.auth.signIn("a@b.com", "p");
    const register = (): Promise<unknown> =>
      subject.auth.register("a@b.com", "p");
    const expectations: Array<[() => Promise<unknown>, string]> = [
      [signIn, "DOMAIN_NOT_ALLOWED"],
      [signIn, "INVALID_CREDENTIALS"],
      [register, "USER_EXISTS"],
      [signIn, "AUTH_UNAVAILABLE"],
      [signIn, "AUTH_UNAVAILABLE"],
      [signIn, "AUTH_UNAVAILABLE"],
    ];
    for (const [operation, code] of expectations) {
      const failure = await operation().catch((error: unknown) => error);
      expect(failure).toBeInstanceOf(PasswordAuthError);
      expect((failure as PasswordAuthError).code).toBe(code);
    }
  });

  it("registration succeeds without storing a session", async () => {
    subject = client([jsonResponse(201, { registered: true })]);
    await subject.auth.register("dev@amazon.com", "secret");
    expect(subject.auth.session()).toBeUndefined();
  });

  it("requests a reset code and confirms the new password", async () => {
    subject = client([
      jsonResponse(200, { sent: true }),
      jsonResponse(200, { reset: true }),
    ]);
    const newPassword = ["n3w", "Secret"].join("-");
    await subject.auth.forgotPassword("dev@amazon.com");
    await subject.auth.resetPassword("dev@amazon.com", "123456", newPassword);
    expect(subject.requests).toEqual([
      { url: "/auth/v1/forgot", body: { email: "dev@amazon.com" } },
      {
        url: "/auth/v1/reset",
        body: {
          email: "dev@amazon.com",
          code: "123456",
          password: newPassword,
        },
      },
    ]);
    expect(subject.auth.session()).toBeUndefined();
  });

  it("maps reset failures onto credential errors", async () => {
    subject = client([
      jsonResponse(400, { code: "INVALID_CODE", message: "Expired." }),
      jsonResponse(429, { code: "TOO_MANY_ATTEMPTS", message: "Later." }),
    ]);
    await expect(
      subject.auth.resetPassword("dev@amazon.com", "000000", "irrelevant"),
    ).rejects.toMatchObject({
      code: "INVALID_CREDENTIALS",
      message: "Expired.",
    });
    await expect(
      subject.auth.forgotPassword("dev@amazon.com"),
    ).rejects.toMatchObject({ code: "AUTH_UNAVAILABLE", message: "Later." });
  });

  it("rejects malformed token payloads", async () => {
    subject = client([jsonResponse(200, { accessToken: "", expiresIn: 0 })]);
    await expect(subject.auth.signIn("a@b.com", "p")).rejects.toMatchObject({
      code: "AUTH_UNAVAILABLE",
    });
  });

  it("refreshes an expiring session and keeps the refresh token", async () => {
    let time = 1_000;
    subject = client(
      [
        jsonResponse(200, TOKENS),
        jsonResponse(200, {
          accessToken: "rotated",
          expiresIn: 3600,
          tokenType: "Bearer",
        }),
      ],
      () => time,
    );
    await subject.auth.signIn("dev@amazon.com", "secret");
    time += 3600 * 1000 - 30_000; // inside the refresh skew
    await expect(subject.auth.accessToken()).resolves.toBe("rotated");
    expect(subject.requests[1]).toEqual({
      url: "/auth/v1/refresh",
      body: { refreshToken: "refresh-token" },
    });
    // The rotated session keeps working without another refresh.
    await expect(subject.auth.accessToken()).resolves.toBe("rotated");
  });

  it("expires the session when refresh is rejected", async () => {
    let time = 1_000;
    subject = client(
      [
        jsonResponse(200, TOKENS),
        jsonResponse(401, { code: "SIGN_IN_FAILED", message: "Expired." }),
      ],
      () => time,
    );
    await subject.auth.signIn("dev@amazon.com", "secret");
    time += 3600 * 1000;
    await expect(subject.auth.accessToken()).rejects.toMatchObject({
      code: "SESSION_EXPIRED",
    });
    expect(subject.auth.session()).toBeUndefined();
  });

  it("propagates outages without treating them as expiry", async () => {
    let time = 1_000;
    subject = client(
      [jsonResponse(200, TOKENS), new Error("offline")],
      () => time,
    );
    await subject.auth.signIn("dev@amazon.com", "secret");
    time += 3600 * 1000;
    await expect(subject.auth.accessToken()).rejects.toMatchObject({
      code: "AUTH_UNAVAILABLE",
    });
  });

  it("requires a session and clears refreshless expiries", async () => {
    await expect(subject.auth.accessToken()).rejects.toMatchObject({
      code: "SESSION_EXPIRED",
    });
    let time = 1_000;
    subject = client(
      [
        jsonResponse(200, {
          accessToken: "only-access",
          expiresIn: 3600,
          tokenType: "Bearer",
        }),
      ],
      () => time,
    );
    await subject.auth.signIn("dev@amazon.com", "secret");
    time += 3600 * 1000;
    await expect(subject.auth.accessToken()).rejects.toMatchObject({
      code: "SESSION_EXPIRED",
    });
  });

  it("drops corrupted or foreign stored sessions", () => {
    subject.storage.setItem(AUTH_STORAGE_KEYS.session, "{not json");
    expect(subject.auth.session()).toBeUndefined();
    subject.storage.setItem(
      AUTH_STORAGE_KEYS.session,
      JSON.stringify({ tokenType: "Basic" }),
    );
    expect(subject.auth.session()).toBeUndefined();
  });

  it("clear removes the stored session", async () => {
    subject = client([jsonResponse(200, TOKENS)]);
    await subject.auth.signIn("dev@amazon.com", "secret");
    subject.auth.clear();
    expect(subject.auth.session()).toBeUndefined();
  });
});
