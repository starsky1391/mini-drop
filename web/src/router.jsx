import { Suspense, lazy } from "react";
import { Spin } from "antd";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import AppLayout from "./components/AppLayout";
import Dashboard from "./pages/Dashboard";

const AuditLogs = lazy(() => import("./pages/AuditLogs"));
const TaskResult = lazy(() => import("./pages/TaskResult"));
const DiagnosisHistory = lazy(() => import("./pages/DiagnosisHistory"));
const AIDiagnosis = lazy(() => import("./pages/AIDiagnosis"));
const AgentDetail = lazy(() => import("./pages/AgentDetail"));
const PersistentWatch = lazy(() => import("./pages/PersistentWatch"));
const Settings = lazy(() => import("./pages/Settings"));

const Lazy = ({ children }) => (
  <Suspense fallback={<Spin size="large" style={{ display: "block", margin: "40px auto" }} />}>
    {children}
  </Suspense>
);

export default function Router() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Dashboard />} />
          <Route
            path="/audit"
            element={<Lazy><AuditLogs /></Lazy>}
          />
          <Route
            path="/task/:taskId"
            element={<Lazy><TaskResult /></Lazy>}
          />
          <Route
            path="/ai-diagnosis"
            element={<Lazy><AIDiagnosis /></Lazy>}
          />
          <Route
            path="/diagnoses"
            element={<Lazy><DiagnosisHistory /></Lazy>}
          />
          <Route
            path="/persistent-watch"
            element={<Lazy><PersistentWatch /></Lazy>}
          />
          <Route
            path="/agent/:agentId"
            element={<Lazy><AgentDetail /></Lazy>}
          />
          <Route
            path="/settings"
            element={<Lazy><Settings /></Lazy>}
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
