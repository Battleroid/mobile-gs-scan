import { create } from "zustand";

interface UiState {
  selectedCaptureId: string | null;
  setSelectedCapture: (id: string | null) => void;
}

export const useUiStore = create<UiState>((set) => ({
  selectedCaptureId: null,
  setSelectedCapture: (id) => set({ selectedCaptureId: id }),
}));
