"use client";

import { useMemo, useState } from "react";
import Link from "next/link";

const items = [
  { name: "Starter plan", state: "active" },
  { name: "Growth plan", state: "active" },
  { name: "Archived order", state: "archived" },
];

export default function ListPage() {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const filtered = useMemo(() => {
    return items.filter((item) => {
      const matchesFilter = filter === "all" ? true : item.state === filter;
      const matchesQuery = query.trim()
        ? item.name.toLowerCase().includes(query.trim().toLowerCase())
        : true;
      return matchesFilter && matchesQuery;
    });
  }, [filter, query]);

  return (
    <main className="shell">
      <header className="hero">
        <p className="eyebrow">List</p>
        <h1>Items</h1>
        <p className="lede">Search, filter, and verify the visible rows.</p>
      </header>
      <section className="panel">
        <div className="actions">
          <input aria-label="Search items" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search items" />
          <button className={filter === "all" ? "primary" : "secondary"} onClick={() => setFilter("all")}>All</button>
          <button className={filter === "active" ? "primary" : "secondary"} onClick={() => setFilter("active")}>Active</button>
          <button className={filter === "archived" ? "primary" : "secondary"} onClick={() => setFilter("archived")}>Archived</button>
          <Link className="secondary" href="/">Back home</Link>
        </div>
        <p className="status">Filtering {filter} items</p>
        <ul className="list">
          {filtered.map((item) => (
            <li key={item.name}>
              {item.name} - {item.state}
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
