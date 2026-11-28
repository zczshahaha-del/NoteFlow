import { useCallback, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  loadCurrentSession,
  login,
  loginWithEmailCode,
  logout,
  register,
  type AuthSession,
} from "../services/auth";
import { sessionRejectedEvent } from "../services/api";

const authSessionQueryKey = ["auth-session"] as const;

export function useAuthSession() {
  const queryClient = useQueryClient();
  const sessionQuery = useQuery<AuthSession | null>({
    queryKey: authSessionQueryKey,
    queryFn: ({ signal }) => loadCurrentSession(signal),
    staleTime: Number.POSITIVE_INFINITY,
  });

  useEffect(() => {
    const recheck = () => {
      if (queryClient.getQueryData(authSessionQueryKey)) {
        void queryClient.invalidateQueries({ queryKey: authSessionQueryKey }, { cancelRefetch: false });
      }
    };
    window.addEventListener(sessionRejectedEvent, recheck);
    return () => window.removeEventListener(sessionRejectedEvent, recheck);
  }, [queryClient]);

  const updateSession = useCallback((nextSession: AuthSession | null) => {
    void queryClient.cancelQueries({ queryKey: authSessionQueryKey });
    queryClient.setQueryData(authSessionQueryKey, nextSession);
  }, [queryClient]);

  const signIn = useCallback(async (email: string, password: string) => {
    updateSession(await login({ email, password }));
  }, [updateSession]);

  const signUp = useCallback(async (email: string, code: string) => {
    updateSession(await register({ email, code }));
  }, [updateSession]);

  const signInWithEmailCode = useCallback(async (email: string, code: string) => {
    updateSession(await loginWithEmailCode(email, code));
  }, [updateSession]);

  const signOut = useCallback(() => {
    void logout();
    updateSession(null);
    queryClient.removeQueries({ queryKey: ["knowledge-base"] });
  }, [queryClient, updateSession]);

  return {
    loading: sessionQuery.isLoading,
    session: sessionQuery.data ?? null,
    signIn,
    signInWithEmailCode,
    signUp,
    signOut,
    updateSession,
  };
}
