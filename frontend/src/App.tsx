import { Moon, Sun } from "lucide-react";
import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { AssignmentTracker } from "./pages/AssignmentTracker";
import { Checklist } from "./pages/Checklist";
import { Dashboard } from "./pages/Dashboard";
import { Quiz } from "./pages/Quiz";
import { Resources } from "./pages/Resources";
import { Settings } from "./pages/Settings";

const navClass = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-1.5 text-sm ${
    isActive
      ? "bg-slate-900 text-white dark:bg-white dark:text-slate-900"
      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
  }`;

function ThemeToggle() {
  // The <html> class is set before first paint by the script in index.html;
  // this state just mirrors it for rendering the right icon.
  const [dark, setDark] = useState(() =>
    document.documentElement.classList.contains("dark"),
  );

  function toggle() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("theme", next ? "dark" : "light");
  }

  return (
    <button
      onClick={toggle}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      title={dark ? "Switch to light theme" : "Switch to dark theme"}
      className="rounded-md p-2 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
    >
      {dark ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}

export default function App() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <nav className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 px-4 py-3">
          <span className="mr-4 font-semibold">Course Copilot</span>
          <NavLink to="/" end className={navClass}>
            Dashboard
          </NavLink>
          <NavLink to="/resources" className={navClass}>
            Resources
          </NavLink>
          <NavLink to="/assignments" className={navClass}>
            Assignments
          </NavLink>
          <NavLink to="/checklist" className={navClass}>
            Checklist
          </NavLink>
          <NavLink to="/quiz" className={navClass}>
            Quiz
          </NavLink>
          {/* Student-built pages (Quiz, Assignment Tracker, your own
              agent's surface) register their routes here. */}
          <span className="ml-auto flex items-center gap-1">
            <NavLink to="/settings" className={navClass}>
              Settings
            </NavLink>
            <ThemeToggle />
          </span>
        </nav>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/resources" element={<Resources />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/assignments" element={<AssignmentTracker />} />
          <Route path="/checklist" element={<Checklist />} />
          <Route path="/quiz" element={<Quiz />} />
        </Routes>
      </main>
    </div>
  );
}
