import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const {
  clearLocalAuthSession,
  getSession,
  push,
  refresh,
  replace,
  signInWithPassword,
} = vi.hoisted(() => ({
  clearLocalAuthSession: vi.fn(),
  getSession: vi.fn(),
  push: vi.fn(),
  refresh: vi.fn(),
  replace: vi.fn(),
  signInWithPassword: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace, refresh }),
}));

vi.mock("@/lib/supabase/client", () => ({
  clearLocalAuthSession,
  createClient: () => ({ auth: { signInWithPassword, getSession } }),
}));

import LoginPage from "@/app/auth/login/page";

describe("LoginPage", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows validation errors instead of submitting an empty form", async () => {
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText("Email is required")).toBeInTheDocument();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("shows a generic invalid-credentials error and never leaks the raw provider error", async () => {
    signInWithPassword.mockResolvedValue({
      error: { status: 400, message: "Invalid login credentials: user not found" },
    });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "wrong-password");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText("Invalid email or password.")).toBeInTheDocument();
    expect(screen.queryByText(/user not found/i)).not.toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("navigates to /app on successful sign in", async () => {
    signInWithPassword.mockResolvedValue({
      data: { session: { access_token: "token" } },
      error: null,
    });
    getSession.mockResolvedValue({
      data: { session: { access_token: "token" } },
    });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "correct-password");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/app"));
    expect(refresh).toHaveBeenCalled();
  });

  it("does not enter the app when Supabase fails to persist the session", async () => {
    signInWithPassword.mockResolvedValue({
      data: { session: { access_token: "token" } },
      error: null,
    });
    getSession.mockResolvedValue({ data: { session: null } });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "correct-password");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(
      await screen.findByText("Sign in did not persist a browser session. Please try again."),
    ).toBeInTheDocument();
    expect(clearLocalAuthSession).toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("LoginPage after a failed email link", () => {
  afterEach(() => {
    window.history.pushState({}, "", "/");
  });

  it("explains that the link failed and points to Forgot password", async () => {
    window.history.pushState({}, "", "/auth/login?error=auth_callback_failed");

    render(<LoginPage />);

    expect(
      await screen.findByText(/that link is invalid or has expired/i),
    ).toBeInTheDocument();
  });

  it("shows nothing for an unknown error value", async () => {
    window.history.pushState({}, "", "/auth/login?error=something_else");

    render(<LoginPage />);

    await waitFor(() =>
      expect(screen.queryByText(/that link is invalid/i)).not.toBeInTheDocument(),
    );
  });
});
