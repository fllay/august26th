import { NextResponse } from "next/server";
import {
  exchangeCodeForToken,
  getGoogleOauthAppBaseUrl,
  saveGoogleOauthToken,
} from "@/lib/server/google-oauth";

export const runtime = "nodejs";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const appBaseUrl = getGoogleOauthAppBaseUrl(request);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const oauthError = url.searchParams.get("error");

  if (oauthError) {
    return NextResponse.redirect(
      new URL(
        `/?google_oauth=error&message=${encodeURIComponent(oauthError)}`,
        appBaseUrl,
      ),
    );
  }

  if (!code) {
    return NextResponse.redirect(
      new URL(
        "/?google_oauth=error&message=Missing%20authorization%20code",
        appBaseUrl,
      ),
    );
  }

  try {
    const token = await exchangeCodeForToken(request, code, state);
    await saveGoogleOauthToken(token);
    return NextResponse.redirect(new URL("/?google_oauth=success", appBaseUrl));
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Google OAuth callback failed.";
    return NextResponse.redirect(
      new URL(
        `/?google_oauth=error&message=${encodeURIComponent(message)}`,
        appBaseUrl,
      ),
    );
  }
}
