// Loads GET /api/assistant/config into the shared ``assistantConfig`` store.
// Its own module so the chat window and Settings → Assistant can both
// reload it without importing each other.

import { api } from "../api.js";
import { assistantConfig } from "../store.js";

export async function loadAssistantConfig() {
    try {
        assistantConfig.set(await api.assistantConfig());
    } catch (err) {
        console.warn("assistant config unavailable", err);
        assistantConfig.set(null);
    }
}
