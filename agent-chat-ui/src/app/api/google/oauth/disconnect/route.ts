import { NextResponse } from "next/server";
import { deleteGoogleOauthToken } from "@/lib/server/google-oauth";

export const runtime = "nodejs";

export async function POST() {
  await deleteGoogleOauthToken();
  return NextResponse.json({ ok: true });
}

