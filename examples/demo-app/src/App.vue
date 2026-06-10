<script setup lang="ts">
import { computed, reactive, ref } from "vue";

type TabId = "home" | "login" | "list" | "form";

const activeTab = ref<TabId>("home");
const login = reactive({
  email: "",
  password: "",
});
const listFilter = ref("all");
const listQuery = ref("");
const form = reactive({
  name: "",
  email: "",
  message: "",
});

const loginStatus = ref("Idle");
const listStatus = ref("Showing all items");
const formStatus = ref("Draft not saved");

const items = [
  { name: "Starter plan", state: "active" },
  { name: "Growth plan", state: "active" },
  { name: "Archived order", state: "archived" },
];

const filteredItems = computed(() => {
  return items.filter((item) => {
    const matchesState = listFilter.value === "all" ? true : item.state === listFilter.value;
    const matchesQuery = listQuery.value.trim()
      ? item.name.toLowerCase().includes(listQuery.value.trim().toLowerCase())
      : true;
    return matchesState && matchesQuery;
  });
});

function openTab(tab: TabId) {
  activeTab.value = tab;
}

function submitLogin() {
  if (!login.email.includes("@")) {
    loginStatus.value = "Enter a valid email address";
    return;
  }
  if (!login.password) {
    loginStatus.value = "Enter a password";
    return;
  }
  loginStatus.value = "Signed in successfully";
}

function setFilter(filter: string) {
  listFilter.value = filter;
  listStatus.value = filter === "all" ? "Showing all items" : `Filtering ${filter} items`;
}

function saveDraft() {
  if (!form.name || !form.email || !form.message) {
    formStatus.value = "Complete the contact form";
    return;
  }
  formStatus.value = "Draft saved successfully";
}
</script>

<template>
  <main class="shell">
    <header class="hero">
      <p class="eyebrow">Checkpoint demo app</p>
      <h1>Workflow coverage for login, list, and form flows</h1>
      <p class="lede">
        This Vue 3 + Vite app is intentionally small, but it gives Checkpoint a stable place to
        run smoke and regression workflows against common UI states.
      </p>

      <nav class="tabs" aria-label="Demo pages">
        <button :class="['tab', activeTab === 'home' && 'active']" @click="openTab('home')">Home</button>
        <button :class="['tab', activeTab === 'login' && 'active']" @click="openTab('login')">Login</button>
        <button :class="['tab', activeTab === 'list' && 'active']" @click="openTab('list')">List</button>
        <button :class="['tab', activeTab === 'form' && 'active']" @click="openTab('form')">Form</button>
      </nav>
    </header>

    <section v-if="activeTab === 'home'" class="panel">
      <h2>Home</h2>
      <p>Checkpoint should see the app title, the tab bar, and the call to action cards.</p>
      <div class="cards">
        <article class="card">
          <h3>Login</h3>
          <p>Sign-in flow with inline validation.</p>
          <button class="link" @click="openTab('login')">Open login</button>
        </article>
        <article class="card">
          <h3>List</h3>
          <p>Filterable item list for smoke and regression coverage.</p>
          <button class="link" @click="openTab('list')">Open list</button>
        </article>
        <article class="card">
          <h3>Form</h3>
          <p>Contact form with save-state feedback.</p>
          <button class="link" @click="openTab('form')">Open form</button>
        </article>
      </div>
    </section>

    <section v-else-if="activeTab === 'login'" class="panel">
      <h2>Sign in to Demo App</h2>
      <label>
        Email
        <input v-model="login.email" name="email" type="email" placeholder="demo@example.com" />
      </label>
      <label>
        Password
        <input v-model="login.password" name="password" type="password" placeholder="••••••••" />
      </label>
      <div class="actions">
        <button class="primary" @click="submitLogin">Sign in</button>
        <button class="secondary" @click="openTab('home')">Cancel</button>
      </div>
      <p class="status">{{ loginStatus }}</p>
    </section>

    <section v-else-if="activeTab === 'list'" class="panel">
      <h2>Items</h2>
      <div class="actions">
        <input v-model="listQuery" aria-label="Search items" placeholder="Search items" />
        <button class="secondary" @click="setFilter('all')">All</button>
        <button class="secondary" @click="setFilter('active')">Active</button>
        <button class="secondary" @click="setFilter('archived')">Archived</button>
      </div>
      <p class="status">{{ listStatus }}</p>
      <ul class="list">
        <li v-for="item in filteredItems" :key="item.name">{{ item.name }} - {{ item.state }}</li>
      </ul>
    </section>

    <section v-else class="panel">
      <h2>Contact form</h2>
      <label>
        Name
        <input v-model="form.name" name="name" placeholder="Ada Lovelace" />
      </label>
      <label>
        Email
        <input v-model="form.email" name="contact-email" type="email" placeholder="ada@example.com" />
      </label>
      <label>
        Message
        <textarea v-model="form.message" name="message" rows="4" placeholder="Write a short message"></textarea>
      </label>
      <div class="actions">
        <button class="primary" @click="saveDraft">Save draft</button>
        <button class="secondary" @click="openTab('home')">Reset</button>
      </div>
      <p class="status">{{ formStatus }}</p>
    </section>
  </main>
</template>

