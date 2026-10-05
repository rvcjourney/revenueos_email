"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowRight, Loader2, LockKeyhole } from "lucide-react";
import { z } from "zod";

import { AuthCard } from "@/app/auth/auth-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { clearLocalAuthSession, createClient } from "@/lib/supabase/client";

const schema = z.object({
  email: z.string().min(1, "Email is required").email("Enter a valid email"),
  password: z.string().min(1, "Password is required"),
});

type FormValues = z.infer<typeof schema>;

export default function LoginPage() {
  const router = useRouter();
  const [formError, setFormError] = useState<string | null>(null);

  // /auth/callback and /auth/confirm send a link they could not verify here.
  // Read from the location, not useSearchParams, which would force this page
  // behind a Suspense boundary.
  useEffect(() => {
    const error = new URLSearchParams(window.location.search).get("error");
    if (error === "auth_callback_failed") {
      setFormError(
        "That link is invalid or has expired. Use Forgot password to get a new one.",
      );
    }
  }, []);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  async function onSubmit(values: FormValues) {
    setFormError(null);
    const supabase = createClient();
    const { data, error } = await supabase.auth.signInWithPassword(values);
    if (error) {
      setFormError(
        error.status === 400
          ? "Invalid email or password."
          : "Sign in failed. Please try again.",
      );
      return;
    }
    if (!data.session?.access_token) {
      await clearLocalAuthSession();
      setFormError("Sign in did not return a usable session. Please try again.");
      return;
    }
    const { data: persisted } = await supabase.auth.getSession();
    if (!persisted.session?.access_token) {
      await clearLocalAuthSession();
      setFormError("Sign in did not persist a browser session. Please try again.");
      return;
    }
    router.replace("/app");
    router.refresh();
  }

  return (
    <AuthCard icon={LockKeyhole} title="Sign in">
      <form
        className="space-y-4"
        onSubmit={handleSubmit(onSubmit)}
        noValidate
      >
        {formError ? <Alert>{formError}</Alert> : null}
        <Field id="email" label="Email" error={errors.email?.message}>
          <Input
            type="email"
            autoComplete="email"
            disabled={isSubmitting}
            {...register("email")}
          />
        </Field>
        <Field id="password" label="Password" error={errors.password?.message}>
          <Input
            type="password"
            autoComplete="current-password"
            disabled={isSubmitting}
            {...register("password")}
          />
        </Field>
        <div className="flex justify-end">
          <Link
            href="/auth/forgot-password"
            className="text-xs font-medium text-brand-700 hover:underline"
          >
            Forgot password?
          </Link>
        </div>
        <Button type="submit" className="w-full" disabled={isSubmitting}>
          {isSubmitting ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <>
              Sign in
              <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </>
          )}
        </Button>
      </form>
      <p className="mt-6 text-center text-sm text-slate-500">
        Don&apos;t have an account?{" "}
        <Link href="/auth/signup" className="font-medium text-brand-700 hover:underline">
          Sign up
        </Link>
      </p>
    </AuthCard>
  );
}
