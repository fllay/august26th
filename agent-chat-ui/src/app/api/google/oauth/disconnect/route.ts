import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import {
  deleteGoogleOauthToken,
  GOOGLE_OAUTH_SESSION_COOKIE_NAME,
  GOOGLE_OAUTH_STATE_COOKIE_NAME,
} from "@/lib/server/google-oauth";

export const runtime = "nodejs";

export async function POST() {
  await deleteGoogleOauthToken();
  const cookieStore = await cookies();
  cookieStore.delete(GOOGLE_OAUTH_SESSION_COOKIE_NAME);
  cookieStore.delete(GOOGLE_OAUTH_STATE_COOKIE_NAME);
  return NextResponse.json({ ok: true });
}
