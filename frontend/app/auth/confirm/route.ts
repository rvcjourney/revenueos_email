import type { EmailOtpType } from "@supabase/supabase-js";
import { NextResponse, type NextRequest } from "next/server";

import { failedLinkUrl, publicOrigin, safeNextPath } from "@/app/auth/redirects";
import { createClient } from "@/lib/supabase/server";

const OTP_TYPES: readonly EmailOtpType[] = [
  "signup",
  "invite",
  "magiclink",
  "recovery",
  "email_change",
  "email",
];

/**
 * Verifies an emailed token hash and signs the user in, then redirects to an
 * approved local destination only.
 *
 * Unlike /auth/callback this needs nothing stored in the browser, so the link
 * works on any device: the one an administrator used to request a password
 * reset for someone else is not the one that person opens it on. The Supabase
 * email template has to point here:
 *   {{ .SiteURL }}/auth/confirm?token_hash={{ .TokenHash }}&type=recovery&next=/auth/reset-password
 */
export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url);
  const tokenHash = searchParams.get("token_hash");
  const type = searchParams.get("type") as EmailOtpType | null;
  const next = safeNextPath(searchParams.get("next"));

  if (tokenHash && type && OTP_TYPES.includes(type)) {
    const supabase = await createClient();
    const { error } = await supabase.auth.verifyOtp({ type, token_hash: tokenHash });
    if (!error) {
      return NextResponse.redirect(`${publicOrigin(request)}${next}`);
    }
  }

  return NextResponse.redirect(failedLinkUrl(request));
}
