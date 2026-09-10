import { Link, NavLink, Route, Routes } from "react-router";

import { Empty } from "./components/Common.js";
import { MemoriesPage } from "./pages/MemoriesPage.js";
import { MemoryPage } from "./pages/MemoryPage.js";
import { PlanPage } from "./pages/PlanPage.js";
import { PlansPage } from "./pages/PlansPage.js";
import { ProjectPage } from "./pages/ProjectPage.js";
import { ProjectsPage } from "./pages/ProjectsPage.js";
import { QueuesPage } from "./pages/QueuesPage.js";
import { SettingsPage } from "./pages/SettingsPage.js";
import { SuggestionPage } from "./pages/SuggestionPage.js";
import { SuggestionsPage } from "./pages/SuggestionsPage.js";

export function App() {
  return (
    <>
      <header>
        <Link to="/" className="brand">
          enggraph
        </Link>
        <nav>
          <NavLink to="/" end>
            Projects
          </NavLink>
          <NavLink to="/plans">Plans</NavLink>
          <NavLink to="/memories">Memories</NavLink>
          <NavLink to="/suggestions">Suggestions</NavLink>
          <NavLink to="/queues">Queues</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<ProjectsPage />} />
          <Route path="/projects/:name" element={<ProjectPage />} />
          <Route path="/plans" element={<PlansPage />} />
          <Route path="/plans/:id" element={<PlanPage />} />
          <Route path="/memories" element={<MemoriesPage />} />
          <Route path="/memories/:id" element={<MemoryPage />} />
          <Route path="/suggestions" element={<SuggestionsPage />} />
          <Route path="/suggestions/:id" element={<SuggestionPage />} />
          <Route path="/queues" element={<QueuesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route
            path="*"
            element={<Empty>There is no such page here.</Empty>}
          />
        </Routes>
      </main>
    </>
  );
}
