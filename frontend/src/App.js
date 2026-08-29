import React from "react";
import "@/index.css";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { LiveProvider } from "@/context/LiveContext";
import { Loader } from "@/components/kit";
import Login from "@/pages/Login";
import CommandCenter from "@/pages/CommandCenter";
import Citizen from "@/pages/Citizen";
import Responder from "@/pages/Responder";
import Facilities from "@/pages/Facilities";
import Comms from "@/pages/Comms";
import Analysis from "@/pages/Analysis";
import Admin from "@/pages/Admin";

const HOME = {
  citizen: "/citizen",
  field_responder: "/responder",
  medical_coordinator: "/medical",
  shelter_coordinator: "/shelter",
  communications_operator: "/comms",
  field_gateway_operator: "/gateway",
  analyst_planner: "/analysis",
  system_administrator: "/admin",
  incident_commander: "/command",
  government_officer: "/command",
};

function Guard({ children, roles }) {
  const { user } = useAuth();
  if (user === null) return <Loader label="VERIFYING CREDENTIALS" />;
  if (user === false) return <Navigate to="/login" replace />;
  if (roles && !roles.includes(user.role)) return <Navigate to={HOME[user.role] || "/command"} replace />;
  return children;
}

function Landing() {
  const { user } = useAuth();
  if (user === null) return <Loader label="STARTING CROS" />;
  if (user === false) return <Navigate to="/login" replace />;
  return <Navigate to={HOME[user.role] || "/command"} replace />;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <LiveProvider>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/" element={<Landing />} />
            <Route path="/command" element={<Guard roles={["incident_commander", "government_officer", "medical_coordinator", "shelter_coordinator", "communications_operator", "analyst_planner"]}><CommandCenter /></Guard>} />
            <Route path="/citizen" element={<Guard><Citizen /></Guard>} />
            <Route path="/responder" element={<Guard roles={["field_responder"]}><Responder /></Guard>} />
            <Route path="/medical" element={<Guard roles={["medical_coordinator", "incident_commander"]}><Facilities kind="hospital" /></Guard>} />
            <Route path="/shelter" element={<Guard roles={["shelter_coordinator", "incident_commander"]}><Facilities kind="shelter" /></Guard>} />
            <Route path="/comms" element={<Guard roles={["communications_operator", "incident_commander"]}><Comms mode="comms" /></Guard>} />
            <Route path="/gateway" element={<Guard roles={["field_gateway_operator", "system_administrator"]}><Comms mode="gateway" /></Guard>} />
            <Route path="/analysis" element={<Guard roles={["analyst_planner", "incident_commander", "government_officer"]}><Analysis /></Guard>} />
            <Route path="/admin" element={<Guard roles={["system_administrator"]}><Admin /></Guard>} />
            <Route path="*" element={<Landing />} />
          </Routes>
        </LiveProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
