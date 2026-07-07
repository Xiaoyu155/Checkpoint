import Link from "next/link";

const cards = [
  { href: "/login", title: "Login", body: "A small sign-in form with inline validation." },
  { href: "/list", title: "List", body: "A searchable list with filter states." },
  { href: "/form", title: "Form", body: "A contact form with save-state feedback." },
];

export default function HomePage() {
  return (
    <main className="shell">
      <header className="hero">
        <p className="eyebrow">Checkpoint Next.js demo</p>
        <h1>Server-rendered workflow demo for login, list, and form pages</h1>
        <p className="lede">
          This app shows a simple Next.js App Router setup with pages that Checkpoint can verify
          across smoke and regression workflows.
        </p>
        <p className="status">Next.js SSR demo ready</p>
      </header>
      <section className="grid" aria-label="Demo pages">
        {cards.map((card) => (
          <article className="card" key={card.href}>
            <h2>{card.title}</h2>
            <p>{card.body}</p>
            <Link className="link" href={card.href}>
              Open {card.title.toLowerCase()}
            </Link>
          </article>
        ))}
      </section>
    </main>
  );
}

