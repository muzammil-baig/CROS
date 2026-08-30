import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, apiError, setToken, getToken } from "../lib/api";
import { setDeviceId, deviceId } from "../lib/localstore";
import { ensureKeypair, hasClientKey } from "../lib/devicecrypto";

const AuthContext = createContext(null);
export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null); // null=checking, false=anon
  const [permissions, setPermissions] = useState([]);
  const [device, setDevice] = useState(null);

  const registerDevice = useCallback(async () => {
    try {
      const material = await ensureKeypair();
      const { data } = await api.post("/auth/device/register", {
        device_type: "web_client",
        label: `browser:${navigator.platform}`,
        public_key: material ? material.public_spki : null,
      });
      setDeviceId(data.device_id);
      setDevice(data);
      localStorage.setItem("cros.device.meta", JSON.stringify(data));
    } catch {
      /* device registration retried on next login */
    }
  }, []);

  const bootstrap = useCallback(async () => {
    if (!getToken()) {
      setUser(false);
      return;
    }
    try {
      const { data } = await api.get("/auth/me");
      setUser(data.user);
      setPermissions(data.permissions || []);
      const meta = localStorage.getItem("cros.device.meta");
      if (meta) setDevice(JSON.parse(meta));
      else registerDevice();
    } catch {
      setToken(null);
      setUser(false);
    }
  }, [registerDevice]);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  const login = async (email, password) => {
    try {
      const { data } = await api.post("/auth/login", { email, password });
      setToken(data.access_token);
      localStorage.setItem("cros.offline_permission_token", data.offline_permission_token);
      setUser(data.user);
      setPermissions(data.permissions || []);
      await registerDevice();
      return { ok: true, user: data.user };
    } catch (e) {
      return { ok: false, error: apiError(e) };
    }
  };

  const logout = async () => {
    try {
      await api.post("/auth/logout");
    } catch {
      /* proceed with local logout regardless */
    }
    setToken(null);
    setUser(false);
    setPermissions([]);
  };

  const can = (perm) => permissions.includes(perm);

  return (
    <AuthContext.Provider
      value={{ user, permissions, can, login, logout, device, deviceId: deviceId(),
               clientSigning: hasClientKey() }}
    >
      {children}
    </AuthContext.Provider>
  );
}
