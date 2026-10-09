import { FormEvent, useState } from "react";
import { Session, login } from "../api";

export function LoginPage({ onLogin }: { onLogin: (s: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await login(username.trim(), password));
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="panel" onSubmit={submit}>
        <h1>Denials Command Center</h1>
        <p className="muted">Gulfview Physician Partners. Contains patient information; sign out when you leave.</p>
        <div style={{ height: 18 }} />
        <div className="field">
          <label htmlFor="u">Username</label>
          <input id="u" type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor="p">Password</label>
          <input id="p" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </div>
        {error && <p className="error" style={{ marginTop: 12 }}>{error}</p>}
        <div style={{ marginTop: 18 }}>
          <button className="primary" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </div>
        <p className="users">
          Demo accounts: <b>manager</b> (Manager); <b>anjali</b>, <b>karan</b>, <b>priya</b>, <b>rahul</b> (Specialists).
          Password is set by DCC_DEMO_PASSWORD (default demo123).
        </p>
      </form>
    </div>
  );
}
