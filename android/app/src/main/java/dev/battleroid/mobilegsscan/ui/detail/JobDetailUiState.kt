package dev.battleroid.mobilegsscan.ui.detail

import androidx.compose.runtime.Immutable
import dev.battleroid.mobilegsscan.StudioClient

/**
 * Snapshot the [JobDetailScreen] composable renders from. Activity
 * polls `/api/jobs/{id}` every 3 s; the log sub-state lives next to
 * it so the screen can show both without coordinating two flows.
 */
@Immutable
data class JobDetailUiState(
    val kind: String,
    val job: StudioClient.JobDetail?,
    val networkError: String?,
    /** Live subprocess log state. The legacy activity auto-opened
     *  this for running/claimed jobs and kept it manually toggleable
     *  afterwards; same semantics here. */
    val log: LogPanelState,
)

@Immutable
data class LogPanelState(
    val open: Boolean,
    /** Tail buffer when available. Empty string when the file
     *  exists but contains nothing yet (the legacy UI renders a
     *  literal "(log file is empty)" placeholder in that case). */
    val content: String?,
    val available: Boolean,
    val fetchError: String?,
) {
    companion object {
        val Initial: LogPanelState = LogPanelState(
            open = false,
            content = null,
            available = false,
            fetchError = null,
        )
    }
}
