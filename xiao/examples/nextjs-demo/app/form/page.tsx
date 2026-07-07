"use client";

import { useState } from "react";
import Link from "next/link";

export default function FormPage() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [message, setMessage] = useState("");
  const [status, setStatus] = useState("Draft not saved");

  function saveDraft() {
    if (!name || !email || !message) {
      setStatus("Complete the contact form");
      return;
    }
    setStatus("Draft saved successfully");
  }

  return (
    <main className="shell">
      <header className="hero">
        <p className="eyebrow">Form</p>
        <h1>Contact form</h1>
        <p className="lede">A compact form workflow with a save-state assertion target.</p>
      </header>
      <section className="panel">
        <label>
          Name
          <input name="name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Ada Lovelace" />
        </label>
        <label>
          Email
          <input name="contact-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="ada@example.com" />
        </label>
        <label>
          Message
          <textarea name="message" rows={4} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Write a short message" />
        </label>
        <div className="actions">
          <button className="primary" onClick={saveDraft}>Save draft</button>
          <Link className="secondary" href="/">Back home</Link>
        </div>
        <p className="status">{status}</p>
      </section>
    </main>
  );
}
