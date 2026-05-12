package dev.battleroid.mobilegsscan.ui.home

import androidx.compose.runtime.Immutable
import dev.battleroid.mobilegsscan.Draft
import dev.battleroid.mobilegsscan.StudioClient

/**
 * Snapshot the [HomeScreen] composable renders from. Kept
 * `@Immutable` so Compose can skip re-render when an unchanged
 * snapshot is re-applied (e.g. a successful poll that returns the
 * same captures list as the previous tick).
 *
 * State production lives in the host activity rather than a
 * ViewModel — there's no surviving-config-change requirement here
 * (the polling restarts on every `onResume`), and the activity is
 * the natural lifecycle owner for the cancellable poll job. A
 * ViewModel can be retrofitted later if the home screen acquires
 * state that needs to survive rotation without a refetch.
 */
@Immutable
data class HomeUiState(
    val status: HomeStatus,
    val studioHost: String?,
    val drafts: List<Draft>,
    val captures: List<StudioClient.Capture>,
    val canCreateNewCapture: Boolean,
)

enum class HomeStatus {
    /** Studio URL is configured and `/api/health` returned 200 on
     *  the most recent poll. */
    Online,

    /** Studio URL is configured but the most recent `/api/health`
     *  failed (network error, non-200, or `/api/captures` threw). */
    Offline,

    /** Studio URL hasn't been set yet — first-run state. The home
     *  screen still works (drafts are local), but the captures
     *  list will stay empty until [ServerConfig.studioUrl] is set. */
    NotConfigured,

    /** Initial state for one poll tick after Online → URL set,
     *  before the first health probe lands. Distinguishes "URL set
     *  but probe pending" from "URL set + probe failed" so the pill
     *  doesn't flash red on first launch. */
    Resolving,
}
