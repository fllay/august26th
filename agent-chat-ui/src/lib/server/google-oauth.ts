import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { cookies } from "next/headers";

const DEFAULT_TOKEN_PATH = path.resolve(
  process.cwd(),
  "../.data/google-oauth.json",
);
export const GOOGLE_OAUTH_SESSION_COOKIE_NAME = "google_oauth_session";
export const GOOGLE_OAUTH_STATE_COOKIE_NAME = "google_oauth_state";
const SCOPES = [
  "openid",
  "https://www.googleapis.com/auth/userinfo.email",
  "https://www.googleapis.com/auth/userinfo.profile",
  "https://www.googleapis.com/auth/forms.body",
  "https://www.googleapis.com/auth/forms.responses.readonly",
  "https://www.googleapis.com/auth/spreadsheets",
  "https://www.googleapis.com/auth/drive",
  "https://www.googleapis.com/auth/script.projects",
  "https://www.googleapis.com/auth/script.deployments",
  "https://www.googleapis.com/auth/script.scriptapp",
];

const GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token";
const GOOGLE_USERINFO_URI = "https://www.googleapis.com/oauth2/v3/userinfo";

export type StoredGoogleProfile = {
  sub?: string;
  name?: string;
  given_name?: string;
  family_name?: string;
  picture?: string;
  email?: string;
  email_verified?: boolean;
};

export type StoredGoogleToken = {
  token?: string;
  refresh_token: string;
  client_id: string;
  client_secret: string;
  token_uri: string;
  scopes: string[];
  scope?: string;
  token_type?: string;
  expiry?: string;
  created_at: string;
  profile?: StoredGoogleProfile;
};

function sanitizeSessionKey(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const normalized = trimmed.replace(/[^a-zA-Z0-9_-]/g, "");
  if (!normalized) return null;
  return normalized.slice(0, 128);
}

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function optionalEnv(name: string): string | null {
  const value = process.env[name]?.trim();
  return value ? value : null;
}

function normalizePublicHostname(hostname: string): string {
  if (hostname === "0.0.0.0" || hostname === "::" || hostname === "[::]") {
    return "localhost";
  }
  return hostname;
}

function getRequestOrigin(request: Request): string {
  const configuredAppUrl = optionalEnv("WEBUI_APP_URL");
  if (configuredAppUrl) {
    return configuredAppUrl.replace(/\/+$/, "");
  }

  const url = new URL(request.url);
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  const forwardedHost = request.headers.get("x-forwarded-host")?.split(",")[0]?.trim();
  const hostHeader = request.headers.get("host")?.trim();

  const protocol = forwardedProto || url.protocol.replace(":", "") || "http";
  const host = forwardedHost || hostHeader || url.host;

  if (!host) {
    return url.origin;
  }

  const normalizedHost = (() => {
    if (host.startsWith("[")) {
      const closingIndex = host.indexOf("]");
      if (closingIndex === -1) return host;
      const hostname = host.slice(0, closingIndex + 1);
      const rest = host.slice(closingIndex + 1);
      return `${normalizePublicHostname(hostname)}${rest}`;
    }

    const [hostname, ...portParts] = host.split(":");
    const port = portParts.length > 0 ? `:${portParts.join(":")}` : "";
    return `${normalizePublicHostname(hostname)}${port}`;
  })();

  return `${protocol}://${normalizedHost}`;
}

async function getOrCreateGoogleOauthSessionKey(): Promise<string> {
  const cookieStore = await cookies();
  const existing = sanitizeSessionKey(
    cookieStore.get(GOOGLE_OAUTH_SESSION_COOKIE_NAME)?.value ?? "",
  );
  if (existing) return existing;

  const sessionKey = randomUUID();
  cookieStore.set(GOOGLE_OAUTH_SESSION_COOKIE_NAME, sessionKey, {
    httpOnly: false,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  });
  return sessionKey;
}

export async function getGoogleOauthSessionKey(): Promise<string> {
  return getOrCreateGoogleOauthSessionKey();
}

export async function getGoogleOauthTokenPath(): Promise<string> {
  const basePath = process.env.GOOGLE_OAUTH_TOKEN_PATH?.trim() || DEFAULT_TOKEN_PATH;
  const sessionKey = await getOrCreateGoogleOauthSessionKey();
  const baseDir = path.dirname(basePath);
  return path.join(baseDir, "google-oauth-sessions", `${sessionKey}.json`);
}

export function getGoogleOauthRedirectUri(request: Request): string {
  return `${getRequestOrigin(request)}/api/google/oauth/callback`;
}

export function getGoogleOauthAppBaseUrl(request: Request): string {
  return getRequestOrigin(request).replace(/\/+$/, "");
}

export async function buildGoogleOauthUrl(request: Request): Promise<string> {
  const clientId = requiredEnv("GOOGLE_CLIENT_ID");
  const redirectUri = getGoogleOauthRedirectUri(request);
  const state = randomUUID();
  const cookieStore = await cookies();

  cookieStore.set(GOOGLE_OAUTH_STATE_COOKIE_NAME, state, {
    httpOnly: true,
    sameSite: "lax",
    secure: redirectUri.startsWith("https://"),
    path: "/",
    maxAge: 60 * 10,
  });

  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: "code",
    access_type: "offline",
    prompt: "consent",
    include_granted_scopes: "true",
    scope: SCOPES.join(" "),
    state,
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`;
}

export async function exchangeCodeForToken(
  request: Request,
  code: string,
  state: string | null,
): Promise<StoredGoogleToken> {
  const cookieStore = await cookies();
  const expectedState = cookieStore.get(GOOGLE_OAUTH_STATE_COOKIE_NAME)?.value;
  cookieStore.delete(GOOGLE_OAUTH_STATE_COOKIE_NAME);

  if (!expectedState || !state || expectedState !== state) {
    throw new Error("Invalid OAuth state. Please try connecting Google again.");
  }

  const payload = new URLSearchParams({
    client_id: requiredEnv("GOOGLE_CLIENT_ID"),
    client_secret: requiredEnv("GOOGLE_CLIENT_SECRET"),
    code,
    grant_type: "authorization_code",
    redirect_uri: getGoogleOauthRedirectUri(request),
  });

  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: payload.toString(),
    cache: "no-store",
  });

  const data = (await response.json()) as {
    access_token?: string;
    error?: string;
    error_description?: string;
    expires_in?: number;
    refresh_token?: string;
    scope?: string;
    token_type?: string;
  };

  if (!response.ok) {
    throw new Error(
      data.error_description ||
        data.error ||
        "Google token exchange failed.",
    );
  }

  if (!data.refresh_token) {
    throw new Error(
      "Google did not return a refresh token. Revoke the app in your Google account and try connecting again.",
    );
  }

  const clientId = requiredEnv("GOOGLE_CLIENT_ID");
  const clientSecret = requiredEnv("GOOGLE_CLIENT_SECRET");
  const scopeList = (data.scope || SCOPES.join(" "))
    .split(" ")
    .map((scope) => scope.trim())
    .filter(Boolean);
  const expiry = data.expires_in
    ? new Date(Date.now() + data.expires_in * 1000).toISOString()
    : undefined;
  const profile =
    data.access_token
      ? await fetchGoogleUserProfile(data.access_token).catch(() => undefined)
      : undefined;

  return {
    token: data.access_token,
    refresh_token: data.refresh_token,
    client_id: clientId,
    client_secret: clientSecret,
    token_uri: GOOGLE_TOKEN_URI,
    scopes: scopeList,
    scope: data.scope,
    token_type: data.token_type,
    expiry,
    created_at: new Date().toISOString(),
    profile,
  };
}

async function fetchGoogleUserProfile(
  accessToken: string,
): Promise<StoredGoogleProfile> {
  const response = await fetch(GOOGLE_USERINFO_URI, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error("Google user profile fetch failed.");
  }
  return (await response.json()) as StoredGoogleProfile;
}

export async function saveGoogleOauthToken(token: StoredGoogleToken) {
  const tokenPath = await getGoogleOauthTokenPath();
  await mkdir(path.dirname(tokenPath), { recursive: true });
  await writeFile(tokenPath, JSON.stringify(token, null, 2), "utf-8");
}

export async function loadGoogleOauthToken(): Promise<StoredGoogleToken | null> {
  try {
    const raw = await readFile(await getGoogleOauthTokenPath(), "utf-8");
    return JSON.parse(raw) as StoredGoogleToken;
  } catch {
    return null;
  }
}

export async function deleteGoogleOauthToken() {
  await rm(await getGoogleOauthTokenPath(), { force: true });
}
