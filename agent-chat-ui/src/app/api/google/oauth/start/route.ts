import { NextResponse } from "next/server";
import {
  buildGoogleOauthUrl,
  getGoogleOauthAppBaseUrl,
} from "@/lib/server/google-oauth";

export const runtime = "nodejs";

export async function GET(request: Request) {
  const appBaseUrl = getGoogleOauthAppBaseUrl(request);
  try {
    const url = await buildGoogleOauthUrl(request);
    return NextResponse.redirect(url);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to start Google OAuth.";
    return NextResponse.redirect(
      new URL(
        `/?google_oauth=error&message=${encodeURIComponent(message)}`,
        appBaseUrl,
      ),
    );
  }
}
