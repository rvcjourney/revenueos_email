import { NextResponse, type NextRequest } from "next/server";

import { failedLinkUrl, publicOrigin, safeNextPath } from "@/app/auth/redirects";
import { createClient } from "@/lib/supabase/server";

/**
 * Exchanges a Supabase auth code (signup confirmation, magic link, or
 * password recovery) for a session, then redirects to an approved local
 * destination only.
 *
 * The code can only be exchanged by the browser that asked for the email (it
 * holds the PKCE verifier). Links meant to be opened anywhere use
 * /auth/confirm instead.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url);
  const code = searchParams.get("code");
  const next = safeNextPath(searchParams.get("next"));

  if (code) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      return NextResponse.redirect(`${publicOrigin(request)}${next}`);
    }
  }

  return NextResponse.redirect(failedLinkUrl(request));
}
