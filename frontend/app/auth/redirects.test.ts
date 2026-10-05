import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { failedLinkUrl, publicOrigin, safeNextPath } from "./redirects";

function request(url: string, headers: Record<string, string> = {}) {
  return new NextRequest(url, { headers });
}

describe("safeNextPath", () => {
  it("keeps an internal path", () => {
    expect(safeNextPath("/auth/reset-password")).toBe("/auth/reset-password");
  });

  it.each([null, "", "https://evil.example", "//evil.example", "/\\evil.example", "app"])(
    "falls back to the app for %s",
    (next) => {
      expect(safeNextPath(next)).toBe("/app");
    },
  );
});

describe("publicOrigin", () => {
  it("uses the host the proxy forwarded, not the container's own address", () => {
    const req = request("http://0.0.0.0:3000/auth/confirm", {
      "x-forwarded-host": "app.example.com",
      "x-forwarded-proto": "https",
    });
    expect(publicOrigin(req)).toBe("https://app.example.com");
  });

  it("defaults a forwarded host to https", () => {
    const req = request("http://0.0.0.0:3000/auth/confirm", {
      "x-forwarded-host": "app.example.com",
    });
    expect(publicOrigin(req)).toBe("https://app.example.com");
  });

  it("falls back to the request's own origin without a proxy", () => {
    expect(publicOrigin(request("http://localhost:3000/auth/confirm"))).toBe(
      "http://localhost:3000",
    );
  });

  it("ignores a forwarded host that is not a plain host name", () => {
    const req = request("http://localhost:3000/auth/confirm", {
      "x-forwarded-host": "evil.example/path?x=",
    });
    expect(publicOrigin(req)).toBe("http://localhost:3000");
  });
});

describe("failedLinkUrl", () => {
  it("sends the user to login on the public origin with a reason", () => {
    const req = request("http://0.0.0.0:3000/auth/confirm", {
      "x-forwarded-host": "app.example.com",
    });
    expect(failedLinkUrl(req).toString()).toBe(
      "https://app.example.com/auth/login?error=auth_callback_failed",
    );
  });
});
