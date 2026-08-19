import { createContext, useContext, useState, ReactNode } from "react";

type ModalState =
  | { type: "run"; runId: string }
  | { type: "candidate"; runId: string; symbol: string }
  | null;

interface ModalContextValue {
  openRunDetail: (runId: string) => void;
  openCandidateDetail: (runId: string, symbol: string) => void;
  closeModal: () => void;
}

const ModalContext = createContext<ModalContextValue | null>(null);

export function useModalActions(): ModalContextValue {
  const ctx = useContext(ModalContext);
  if (!ctx) throw new Error("useModalActions must be used within ModalProvider");
  return ctx;
}

export function useModalState() {
  const [state, setState] = useState<ModalState>(null);
  const value: ModalContextValue = {
    openRunDetail: (runId) => setState({ type: "run", runId }),
    openCandidateDetail: (runId, symbol) => setState({ type: "candidate", runId, symbol }),
    closeModal: () => setState(null),
  };
  return { state, setState, value };
}

export function ModalProvider({
  value,
  children,
}: {
  value: ModalContextValue;
  children: ReactNode;
}) {
  return <ModalContext.Provider value={value}>{children}</ModalContext.Provider>;
}

export type { ModalState };
