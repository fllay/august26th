import { NextResponse } from "next/server";
import { loadGoogleOauthToken } from "@/lib/server/google-oauth";

export const runtime = "nodejs";

export async function GET() {
  const token = await loadGoogleOauthToken();
  return NextResponse.json({
    connected: !!token?.refresh_token,
    connectedAt: token?.created_at ?? null,
    scope: token?.scope ?? null,
  });
}

