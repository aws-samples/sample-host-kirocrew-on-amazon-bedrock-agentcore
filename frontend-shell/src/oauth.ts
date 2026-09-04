const TRANSACTION_KEY = "kirocrew.agentcore.oauth.transaction";
const SESSION_KEY = "kirocrew.agentcore.oauth.session";
const CALLBACK_MAX_AGE_MS = 10 * 60 * 1000;
const REFRESH_SKEW_MS = 60 * 1000;

export interface CognitoOAuthConfig {
  readonly authorizationEndpoint: string;
  readonly tokenEndpoint: string;
  readonly logoutEndpoint: string;
  readonly clientId: string;
  readonly redirectUri: string;
  readonly logoutUri: string;
  readonly scope: string;
}

export interface OAuthSession {
  readonly accessToken: string;
  readonly refreshToken?: string;
  readonly expiresAt: number;
  readonly tokenType: "Bearer";
  readonly scope: string;
}

interface OAuthTransaction {
  readonly state: string;
  readonly verifier: string;
  readonly createdAt: number;
  readonly returnTo: string;
}

interface TokenResponse {
  readonly access_token: string;
  readonly refresh_token?: string;
  readonly expires_in: number;
  readonly token_type: string;
  readonly scope?: string;
}

export type OAuthErrorCode =
  | "CONFIGURATION"
  | "CALLBACK_REJECTED"
  | "STATE_MISMATCH"
  | "TOKEN_EXCHANGE_FAILED"
  | "SESSION_EXPIRED";

export class OAuthError extends Error {
  public constructor(
    public readonly code: OAuthErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "OAuthError";
  }
}

export interface OAuthClientOptions {
  readonly storage?: Storage;
  readonly fetch?: typeof fetch;
  readonly crypto?: Crypto;
  readonly now?: () => number;
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

function parseAbsoluteHttpsUrl(value: string, field: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new OAuthError("CONFIGURATION", `${field} must be an absolute URL.`);
  }
  if (url.protocol !== "https:" || url.username || url.password || url.hash) {
    throw new OAuthError("CONFIGURATION", `${field} must use HTTPS.`);
  }
  return url;
}

function validateConfig(config: CognitoOAuthConfig): void {
  const authorization = parseAbsoluteHttpsUrl(
    config.authorizationEndpoint,
    "Authorization endpoint",
  );
  const token = parseAbsoluteHttpsUrl(config.tokenEndpoint, "Token endpoint");
  const logout = parseAbsoluteHttpsUrl(
    config.logoutEndpoint,
    "Logout endpoint",
  );
  const redirect = parseAbsoluteHttpsUrl(config.redirectUri, "Redirect URI");
  const logoutUri = parseAbsoluteHttpsUrl(config.logoutUri, "Logout URI");
  if (
    authorization.origin !== token.origin ||
    authorization.origin !== logout.origin ||
    redirect.origin !== logoutUri.origin ||
    !config.clientId ||
    !config.scope.split(/\s+/u).includes("kirocrew.control/invoke")
  ) {
    throw new OAuthError(
      "CONFIGURATION",
      "Cognito OAuth configuration is inconsistent.",
    );
  }
}

function isTokenResponse(value: unknown): value is TokenResponse {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.access_token === "string" &&
    record.access_token.length > 0 &&
    typeof record.expires_in === "number" &&
    Number.isFinite(record.expires_in) &&
    record.expires_in > 0 &&
    record.token_type === "Bearer" &&
    (record.refresh_token === undefined ||
      typeof record.refresh_token === "string") &&
    (record.scope === undefined || typeof record.scope === "string")
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

export class CognitoPkceClient {
  readonly #config: CognitoOAuthConfig;
  readonly #storage: Storage;
  readonly #fetch: typeof fetch;
  readonly #crypto: Crypto;
  readonly #now: () => number;

  public constructor(
    config: CognitoOAuthConfig,
    options: OAuthClientOptions = {},
  ) {
    validateConfig(config);
    this.#config = config;
    this.#storage = options.storage ?? sessionStorage;
    this.#fetch = options.fetch ?? fetch;
    this.#crypto = options.crypto ?? crypto;
    this.#now = options.now ?? Date.now;
  }

  public async createAuthorizationUrl(returnTo = "/"): Promise<URL> {
    if (!returnTo.startsWith("/") || returnTo.startsWith("//")) {
      throw new OAuthError("CONFIGURATION", "Return path is invalid.");
    }
    const verifier = base64Url(this.#randomBytes(32));
    const state = base64Url(this.#randomBytes(32));
    const challenge = base64Url(
      new Uint8Array(
        await this.#crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(verifier),
        ),
      ),
    );
    const transaction: OAuthTransaction = {
      state,
      verifier,
      createdAt: this.#now(),
      returnTo,
    };
    this.#storage.setItem(TRANSACTION_KEY, JSON.stringify(transaction));
    const url = new URL(this.#config.authorizationEndpoint);
    url.search = new URLSearchParams({
      client_id: this.#config.clientId,
      code_challenge: challenge,
      code_challenge_method: "S256",
      redirect_uri: this.#config.redirectUri,
      response_type: "code",
      scope: this.#config.scope,
      state,
    }).toString();
    return url;
  }

  public async handleCallback(callback: URL): Promise<{
    readonly session: OAuthSession;
    readonly returnTo: string;
  }> {
    const transaction = readJson<OAuthTransaction>(
      this.#storage,
      TRANSACTION_KEY,
    );
    this.#storage.removeItem(TRANSACTION_KEY);
    const callbackError = callback.searchParams.get("error");
    const state = callback.searchParams.get("state");
    const code = callback.searchParams.get("code");
    if (callbackError !== null) {
      throw new OAuthError(
        "CALLBACK_REJECTED",
        "Cognito did not complete sign-in.",
      );
    }
    if (
      transaction === undefined ||
      typeof transaction.state !== "string" ||
      typeof transaction.verifier !== "string" ||
      typeof transaction.createdAt !== "number" ||
      typeof transaction.returnTo !== "string" ||
      state === null ||
      state !== transaction.state
    ) {
      throw new OAuthError(
        "STATE_MISMATCH",
        "The sign-in response could not be verified. Start sign-in again.",
      );
    }
    if (
      this.#now() - transaction.createdAt > CALLBACK_MAX_AGE_MS ||
      code === null ||
      code.length === 0
    ) {
      throw new OAuthError(
        "CALLBACK_REJECTED",
        "The sign-in response expired. Start sign-in again.",
      );
    }
    const token = await this.#requestToken(
      new URLSearchParams({
        client_id: this.#config.clientId,
        code,
        code_verifier: transaction.verifier,
        grant_type: "authorization_code",
        redirect_uri: this.#config.redirectUri,
      }),
    );
    const session = this.#toSession(token);
    this.#storeSession(session);
    return { session, returnTo: transaction.returnTo };
  }

  public session(): OAuthSession | undefined {
    const value = readJson<OAuthSession>(this.#storage, SESSION_KEY);
    if (
      value === undefined ||
      typeof value.accessToken !== "string" ||
      typeof value.expiresAt !== "number" ||
      value.tokenType !== "Bearer" ||
      typeof value.scope !== "string"
    ) {
      this.#storage.removeItem(SESSION_KEY);
      return undefined;
    }
    return value;
  }

  public async accessToken(): Promise<string> {
    const current = this.session();
    if (current === undefined) {
      throw new OAuthError("SESSION_EXPIRED", "Sign-in is required.");
    }
    if (current.expiresAt - this.#now() > REFRESH_SKEW_MS) {
      return current.accessToken;
    }
    if (current.refreshToken === undefined) {
      this.clear();
      throw new OAuthError("SESSION_EXPIRED", "Sign-in has expired.");
    }
    const token = await this.#requestToken(
      new URLSearchParams({
        client_id: this.#config.clientId,
        grant_type: "refresh_token",
        refresh_token: current.refreshToken,
      }),
    );
    const refreshed = this.#toSession(token, current.refreshToken);
    this.#storeSession(refreshed);
    return refreshed.accessToken;
  }

  public logoutUrl(): URL {
    this.clear();
    const url = new URL(this.#config.logoutEndpoint);
    url.search = new URLSearchParams({
      client_id: this.#config.clientId,
      logout_uri: this.#config.logoutUri,
    }).toString();
    return url;
  }

  public clear(): void {
    this.#storage.removeItem(TRANSACTION_KEY);
    this.#storage.removeItem(SESSION_KEY);
  }

  async #requestToken(body: URLSearchParams): Promise<TokenResponse> {
    let response: Response;
    try {
      response = await this.#fetch(this.#config.tokenEndpoint, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body,
        credentials: "omit",
        referrerPolicy: "no-referrer",
      });
    } catch {
      throw new OAuthError(
        "TOKEN_EXCHANGE_FAILED",
        "Sign-in could not be completed. Try again.",
      );
    }
    if (!response.ok) {
      throw new OAuthError(
        "TOKEN_EXCHANGE_FAILED",
        "Sign-in could not be completed. Try again.",
      );
    }
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      value = undefined;
    }
    if (!isTokenResponse(value)) {
      throw new OAuthError(
        "TOKEN_EXCHANGE_FAILED",
        "Sign-in returned an invalid response.",
      );
    }
    return value;
  }

  #toSession(token: TokenResponse, refreshToken?: string): OAuthSession {
    const nextRefresh = token.refresh_token ?? refreshToken;
    const required = {
      accessToken: token.access_token,
      expiresAt: this.#now() + token.expires_in * 1000,
      tokenType: "Bearer" as const,
      scope: token.scope ?? this.#config.scope,
    };
    return nextRefresh === undefined
      ? required
      : { ...required, refreshToken: nextRefresh };
  }

  #storeSession(session: OAuthSession): void {
    this.#storage.setItem(SESSION_KEY, JSON.stringify(session));
  }

  #randomBytes(length: number): Uint8Array {
    return this.#crypto.getRandomValues(new Uint8Array(length));
  }
}

export const OAUTH_STORAGE_KEYS = Object.freeze({
  session: SESSION_KEY,
  transaction: TRANSACTION_KEY,
});
