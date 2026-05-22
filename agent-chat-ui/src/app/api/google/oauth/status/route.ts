import { NextResponse } from "next/server";
import {
  getGoogleOauthSessionKey,
  loadGoogleOauthToken,
} from "@/lib/server/google-oauth";

export const runtime = "nodejs";

export async function GET() {
  const token = await loadGoogleOauthToken();
  const sessionKey = await getGoogleOauthSessionKey();
  return NextResponse.json({
    connected: !!token?.refresh_token,
    connectedAt: token?.created_at ?? null,
    scope: token?.scope ?? null,
    sessionKey,
    profile: token?.profile ?? null,
  });
}
