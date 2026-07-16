"use client";

import { useState } from "react";
import Link from "next/link";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState("Idle");

  function submit() {
    if (!email.includes("@")) {
      setStatus("Enter a valid email address");
      return;
    }
    if (!password) {
      setStatus("Enter a password");
      return;
    }
    setStatus("Signed in successfully");
  }

  return (
    <main className="shell">
      <header className="hero">
        <p className="eyebrow">Login</p>
        <h1>Sign in to Next.js Demo</h1>
        <p className="lede">This page is a stable target for login smoke and regression workflows.</p>
      </header>
      <section className="panel">
        <label>
          Email
          <input name="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="demo@example.com" />
        </label>
        <label>
          Password
          <input name="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="••••••••" />
        </label>
        <div className="actions">
          <button className="primary" onClick={submit}>Sign in</button>
          <Link className="secondary" href="/">Back home</Link>
        </div>
        <p className="status">{status}</p>
      </section>
    </main>
  );
}
