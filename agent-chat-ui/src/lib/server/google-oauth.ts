import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { cookies } from "next/headers";

const DEFAULT_TOKEN_PATH = path.resolve(
  process.cwd(),
  "../.data/google-oauth.json",
);
const STATE_COOKIE_NAME = "google_oauth_state";
const SCOPES = [
  "https://www.googleapis.com/auth/forms.body",
  "https://www.googleapis.com/auth/forms.responses.readonly",
  "https://www.googleapis.com/auth/drive.file",
];

export type StoredGoogleToken = {
  refresh_token: string;
  scope?: string;
  token_type?: string;
  created_at: string;
};

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

export function getGoogleOauthTokenPath(): string {
  return process.env.GOOGLE_OAUTH_TOKEN_PATH?.trim() || DEFAULT_TOKEN_PATH;
}

export function getGoogleOauthRedirectUri(request: Request): string {
  const configured = process.env.GOOGLE_OAUTH_REDIRECT_URI?.trim();
  if (configured) {
    return configured;
  }
  return new URL("/api/google/oauth/callback", request.url).toString();
}

export function getGoogleOauthAppBaseUrl(request: Request): string {
  const configured =
    process.env.WEBUI_PUBLIC_APP_URL?.trim() ||
    process.env.NEXT_PUBLIC_APP_URL?.trim();
  if (configured) {
    return configured.replace(/\/+$/, "");
  }

  const url = new URL(request.url);
  if (url.hostname === "0.0.0.0") {
    url.hostname = "localhost";
  }
  url.pathname = "/";
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/+$/, "");
}

export async function buildGoogleOauthUrl(request: Request): Promise<string> {
  const clientId = requiredEnv("GOOGLE_CLIENT_ID");
  const redirectUri = getGoogleOauthRedirectUri(request);
  const state = randomUUID();
  const cookieStore = await cookies();

  cookieStore.set(STATE_COOKIE_NAME, state, {
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
  const expectedState = cookieStore.get(STATE_COOKIE_NAME)?.value;
  cookieStore.delete(STATE_COOKIE_NAME);

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
    error?: string;
    error_description?: string;
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

  return {
    refresh_token: data.refresh_token,
    scope: data.scope,
    token_type: data.token_type,
    created_at: new Date().toISOString(),
  };
}

export async function saveGoogleOauthToken(token: StoredGoogleToken) {
  const tokenPath = getGoogleOauthTokenPath();
  await mkdir(path.dirname(tokenPath), { recursive: true });
  await writeFile(tokenPath, JSON.stringify(token, null, 2), "utf-8");
}

export async function loadGoogleOauthToken(): Promise<StoredGoogleToken | null> {
  try {
    const raw = await readFile(getGoogleOauthTokenPath(), "utf-8");
    return JSON.parse(raw) as StoredGoogleToken;
  } catch {
    return null;
  }
}

export async function deleteGoogleOauthToken() {
  await rm(getGoogleOauthTokenPath(), { force: true });
}
