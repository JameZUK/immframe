/* immframe login page — exchanges the dashboard credentials for a session
 * cookie (POST /api/login) and goes back to where the user was heading. */
"use strict";

const $ = id => document.getElementById(id);

function target() {
  const next = new URLSearchParams(location.search).get("next");
  // Same-site paths only — never bounce to another origin.
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
}

function showError(text) {
  $("error-text").textContent = text;
  $("error").hidden = false;
}

async function checkAlreadySignedIn() {
  try {
    const r = await fetch("/api/session", { cache: "no-store" });
    const s = await r.json();
    if (s.authenticated) location.replace(target());
  } catch (e) { /* stay on the form */ }
}

$("toggle-pw").addEventListener("click", () => {
  const pw = $("password");
  const show = pw.type === "password";
  pw.type = show ? "text" : "password";
  $("pw-icon").setAttribute("href", show ? "#i-eye-off" : "#i-eye");
  $("toggle-pw").setAttribute("aria-label", show ? "Hide password" : "Show password");
  pw.focus();
});

$("login-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  $("error").hidden = true;
  const username = $("username").value.trim();
  const password = $("password").value;
  if (!username || !password) { showError("Enter your username and password."); return; }
  const btn = $("submit");
  btn.disabled = true;
  btn.replaceChildren(Object.assign(document.createElement("span"), { className: "spinner" }));
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, remember: $("remember").checked }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) throw new Error((data && data.error) || `Sign-in failed (HTTP ${r.status})`);
    location.replace(target());
  } catch (e) {
    showError(e.message === "wrong username or password" ? "Wrong username or password." : e.message);
    $("password").select();
    btn.disabled = false;
    btn.replaceChildren(Object.assign(document.createElement("span"), { textContent: "Sign in" }));
  }
});

checkAlreadySignedIn();
