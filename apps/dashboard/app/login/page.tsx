"use client";

import { ShieldCheck } from "@phosphor-icons/react";
import { FormEvent, useEffect, useState } from "react";

import { API_BASE_URL } from "@/lib/api";
import { Session, getSession, homeFor, setSession } from "@/lib/auth";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Already signed in: straight to the right home.
  useEffect(() => {
    const session = getSession();
    if (session) window.location.replace(homeFor(session.role));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        setError(typeof body.detail === "string" ? body.detail : "Couldn't sign in.");
        return;
      }
      setSession(body as Session);
      window.location.replace(homeFor((body as Session).role));
    } catch {
      setError("Can't reach the server. Is the API running?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="login-card surface" onSubmit={submit}>
        <div className="login-brand">
          <span className="brand-mark" aria-hidden>
            <ShieldCheck size={18} weight="bold" />
          </span>
          <span>Boundary</span>
        </div>
        <h1>Sign in</h1>
        <label className="field">
          <span className="label">Username</span>
          <input
            className="input"
            autoComplete="username"
            autoFocus
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="field">
          <span className="label">Password</span>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        <button className="btn btn-primary login-submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
