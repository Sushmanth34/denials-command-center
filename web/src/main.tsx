import React, { createContext, useContext, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import "./styles.css";
import { Session, api, loadSession, saveSession, setUnauthorizedHandler } from "./api";
import { LoginPage } from "./pages/Login";
import { QueuePage } from "./pages/Queue";
import { ClaimPage } from "./pages/Claim";
import { MondayPage } from "./pages/Monday";
import { ReviewPage } from "./pages/Review";
import { PreventionPage } from "./pages/Prevention";
import { ReconciliationPage } from "./pages/Reconciliation";

type AuthCtx = { session: Session | null; setSession: (s: Session | null) => void };
const Auth = createContext<AuthCtx>({ session: null, setSession: () => {} });
export const useAuth = () => useContext(Auth);

function App() {
  const [session, setSessionState] = useState<Session | null>(loadSession());
  const setSession = (s: Session | null) => {
    saveSession(s);
    setSessionState(s);
  };
  useEffect(() => setUnauthorizedHandler(() => setSession(null)), []);

  return (
    <Auth.Provider value={{ session, setSession }}>
      <BrowserRouter>
        {session ? (
          <Shell />
        ) : (
          <Routes>
            <Route path="*" element={<LoginPage onLogin={setSession} />} />
          </Routes>
        )}
      </BrowserRouter>
    </Auth.Provider>
  );
}

function Shell() {
  const { session, setSession } = useAuth();
  const navigate = useNavigate();
  const isManager = session!.role === "Manager";
  const [reviewCount, setReviewCount] = useState<number | null>(null);
  useEffect(() => {
    api<unknown[]>("/review-queue").then((r) => setReviewCount(r.length)).catch(() => setReviewCount(null));
  }, []);

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <strong>Denials Command Center</strong>
          <span>Gulfview Physician Partners</span>
        </div>
        <nav className="nav" aria-label="Main">
          {isManager && <NavLink to="/" end>Monday report</NavLink>}
          <NavLink to={isManager ? "/queue" : "/"} end>{isManager ? "Team queue" : "My queue"}</NavLink>
          <NavLink to="/review">
            Needs review {reviewCount ? <span className="count">{reviewCount}</span> : null}
          </NavLink>
          <NavLink to="/prevention">Prevention rules</NavLink>
          {isManager && <NavLink to="/reconciliation">Reconciliation</NavLink>}
        </nav>
        <div className="who">
          <strong>{session!.displayName}</strong>
          <span className="muted">{session!.role === "Manager" ? "Manager" : "Denials specialist"}</span>
          <div style={{ marginTop: 8 }}>
            <button onClick={() => { setSession(null); navigate("/"); }}>Sign out</button>
          </div>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={isManager ? <MondayPage /> : <QueuePage />} />
          <Route path="/queue" element={<QueuePage />} />
          <Route path="/claims/:claimId" element={<ClaimPage />} />
          <Route path="/review" element={<ReviewPage onChange={setReviewCount} />} />
          <Route path="/prevention" element={<PreventionPage />} />
          <Route path="/reconciliation" element={isManager ? <ReconciliationPage /> : <Navigate to="/" />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
