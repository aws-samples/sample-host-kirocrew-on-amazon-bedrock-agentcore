import { durableStorage } from "./storage.js";

const SESSION_KEY = "kirocrew.agentcore.auth.session";
const REFRESH_SKEW_MS = 60 * 1000;

/**
 * Password authentication against the deployment's gated auth API.
 *
 * The browser never talks to Cognito: registration and sign-in go to the
 * same-origin `/auth/v1/*` routes, where a Lambda enforces the allowed email
 * domains and performs the Cognito calls with administrator credentials. The
 * stored session mirrors the OAuth session shape so downstream consumers are
 * indifferent to how the tokens were obtained.
 */
export interface PasswordAuthConfig {
  readonly basePath: string;
  readonly allowedDomains?: readonly string[];
}

export interface AuthSession {
  readonly accessToken: string;
  readonly refreshToken?: string;
  readonly expiresAt: number;
  readonly tokenType: "Bearer";
}

export type PasswordAuthErrorCode =
  | "CONFIGURATION"
  | "INVALID_CREDENTIALS"
  | "DOMAIN_NOT_ALLOWED"
  | "EMAIL_NOT_VERIFIED"
  | "USER_EXISTS"
  | "AUTH_UNAVAILABLE"
  | "SESSION_EXPIRED";

export class PasswordAuthError extends Error {
  public constructor(
    public readonly code: PasswordAuthErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "PasswordAuthError";
  }
}

export interface PasswordAuthClientOptions {
  readonly storage?: Storage;
  readonly fetch?: typeof fetch;
  readonly now?: () => number;
}

interface TokenPayload {
  readonly accessToken: string;
  readonly refreshToken?: string;
  readonly expiresIn: number;
  readonly tokenType: "Bearer";
}

interface ErrorPayload {
  readonly code?: string;
  readonly message?: string;
}

function isTokenPayload(value: unknown): value is TokenPayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.accessToken === "string" &&
    record.accessToken.length > 0 &&
    typeof record.expiresIn === "number" &&
    Number.isFinite(record.expiresIn) &&
    record.expiresIn > 0 &&
    record.tokenType === "Bearer" &&
    (record.refreshToken === undefined ||
      typeof record.refreshToken === "string")
  );
}

function readJson<T>(storage: Storage, key: string): T | undefined {
  const raw = storage.getItem(key);
  if (raw === null) {
    return undefined;
  }
  try {
    return JSON.parse(raw) as T;
  } catch {
    storage.removeItem(key);
    return undefined;
  }
}

const CLIENT_ERROR_CODES: Readonly<Record<string, PasswordAuthErrorCode>> = {
  DOMAIN_NOT_ALLOWED: "DOMAIN_NOT_ALLOWED",
  EMAIL_NOT_VERIFIED: "EMAIL_NOT_VERIFIED",
  INVALID_CODE: "INVALID_CREDENTIALS",
  INVALID_EMAIL: "INVALID_CREDENTIALS",
  INVALID_PASSWORD: "INVALID_CREDENTIALS",
  INVALID_REQUEST: "INVALID_CREDENTIALS",
  SIGN_IN_FAILED: "INVALID_CREDENTIALS",
  USER_EXISTS: "USER_EXISTS",
};

/** Endpoints that answer with a plain acknowledgement instead of tokens. */
const TOKENLESS_PATHS: ReadonlySet<string> = new Set([
  "/register",
  "/resend",
  "/forgot",
  "/reset",
]);

export class PasswordAuthClient {
  readonly #basePath: string;
  readonly #storage: Storage;
  readonly #fetch: typeof fetch;
  readonly #now: () => number;

  public constructor(
    config: PasswordAuthConfig,
    options: PasswordAuthClientOptions = {},
  ) {
    if (!config.basePath.startsWith("/") || config.basePath.startsWith("//")) {
      throw new PasswordAuthError(
        "CONFIGURATION",
        "The auth base path must be same-origin.",
      );
    }
    this.#basePath = config.basePath.replace(/\/$/u, "");
    this.#storage = options.storage ?? durableStorage();
    this.#fetch = options.fetch ?? fetch;
    this.#now = options.now ?? Date.now;
  }

  public async register(email: string, password: string): Promise<void> {
    await this.#request("/register", { email, password });
  }

  /** Verify the emailed registration code; success signs the user in. */
  public async confirmEmail(
    email: string,
    password: string,
    code: string,
  ): Promise<AuthSession> {
    const token = await this.#request("/confirm", { email, password, code });
    const session = this.#toSession(token);
    this.#storage.setItem(SESSION_KEY, JSON.stringify(session));
    return session;
  }

  public async resendCode(email: string, password: string): Promise<void> {
    await this.#request("/resend", { email, password });
  }

  /** Ask Cognito to email a reset code; silent about account existence. */
  public async forgotPassword(email: string): Promise<void> {
    await this.#request("/forgot", { email });
  }

  public async resetPassword(
    email: string,
    code: string,
    password: string,
  ): Promise<void> {
    await this.#request("/reset", { email, code, password });
  }

  public async signIn(email: string, password: string): Promise<AuthSession> {
    const token = await this.#request("/login", { email, password });
    const session = this.#toSession(token);
    this.#storage.setItem(SESSION_KEY, JSON.stringify(session));
    return session;
  }

  public session(): AuthSession | undefined {
    const value = readJson<AuthSession>(this.#storage, SESSION_KEY);
    if (
      value === undefined ||
      typeof value.accessToken !== "string" ||
      typeof value.expiresAt !== "number" ||
      value.tokenType !== "Bearer"
    ) {
      this.#storage.removeItem(SESSION_KEY);
      return undefined;
    }
    return value;
  }

  public async accessToken(): Promise<string> {
    const current = this.session();
    if (current === undefined) {
      throw new PasswordAuthError("SESSION_EXPIRED", "Sign-in is required.");
    }
    if (current.expiresAt - this.#now() > REFRESH_SKEW_MS) {
      return current.accessToken;
    }
    if (current.refreshToken === undefined) {
      this.clear();
      throw new PasswordAuthError("SESSION_EXPIRED", "Sign-in has expired.");
    }
    let token: TokenPayload;
    try {
      token = await this.#request("/refresh", {
        refreshToken: current.refreshToken,
      });
    } catch (error: unknown) {
      this.clear();
      if (
        error instanceof PasswordAuthError &&
        error.code === "AUTH_UNAVAILABLE"
      ) {
        throw error;
      }
      throw new PasswordAuthError("SESSION_EXPIRED", "Sign-in has expired.");
    }
    const refreshed = this.#toSession(token, current.refreshToken);
    this.#storage.setItem(SESSION_KEY, JSON.stringify(refreshed));
    return refreshed.accessToken;
  }

  public clear(): void {
    this.#storage.removeItem(SESSION_KEY);
  }

  async #request(
    path: string,
    body: Record<string, string>,
  ): Promise<TokenPayload> {
    let response: Response;
    try {
      response = await this.#fetch(`${this.#basePath}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        credentials: "omit",
        referrerPolicy: "no-referrer",
      });
    } catch {
      throw new PasswordAuthError(
        "AUTH_UNAVAILABLE",
        "The sign-in service could not be reached. Try again.",
      );
    }
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      value = undefined;
    }
    if (!response.ok) {
      const payload = (value ?? {}) as ErrorPayload;
      const code =
        payload.code !== undefined
          ? (CLIENT_ERROR_CODES[payload.code] ?? "AUTH_UNAVAILABLE")
          : "AUTH_UNAVAILABLE";
      const message =
        typeof payload.message === "string" && payload.message.length > 0
          ? payload.message
          : "Sign-in could not be completed. Try again.";
      throw new PasswordAuthError(code, message);
    }
    if (TOKENLESS_PATHS.has(path)) {
      return { accessToken: "-", expiresIn: 1, tokenType: "Bearer" };
    }
    if (!isTokenPayload(value)) {
      throw new PasswordAuthError(
        "AUTH_UNAVAILABLE",
        "Sign-in returned an invalid response.",
      );
    }
    return value;
  }

  #toSession(token: TokenPayload, refreshToken?: string): AuthSession {
    const nextRefresh = token.refreshToken ?? refreshToken;
    const required = {
      accessToken: token.accessToken,
      expiresAt: this.#now() + token.expiresIn * 1000,
      tokenType: "Bearer" as const,
    };
    return nextRefresh === undefined
      ? required
      : { ...required, refreshToken: nextRefresh };
  }
}

export const AUTH_STORAGE_KEYS = Object.freeze({
  session: SESSION_KEY,
});
