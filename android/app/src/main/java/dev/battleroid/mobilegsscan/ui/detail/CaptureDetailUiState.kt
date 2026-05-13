package dev.battleroid.mobilegsscan.ui.detail

import androidx.compose.runtime.Immutable
import dev.battleroid.mobilegsscan.StudioClient

/**
 * Snapshot the [CaptureDetailScreen] composable renders from.
 * Sourced from two polling loops the legacy [CaptureDetailActivity]
 * ran: `/api/captures/{id}` for the capture row + `/api/scenes/{id}`
 * for the pipeline jobs (once a scene exists).
 *
 * Status is split out from [capture] so the chip header can flip
 * between online/offline tones without having to thread the
 * exception through capture.error (which is server-side capture
 * error, not network error).
 */
@Immutable
data class CaptureDetailUiState(
    val captureName: String,
    val capture: StudioClient.Capture?,
    /** Set once `capture.scene_id` resolves and the scene fetch
     *  succeeds. Null while the scene is still being created, or
     *  if the scene fetch transiently failed. */
    val scene: StudioClient.Scene?,
    /** True while there's no scene yet (capture freshly finalized,
     *  worker hasn't created the scene row). Distinguishes "no
     *  pipeline visible" from "scene exists but jobs list is
     *  empty" (which shouldn't happen but is harmless). */
    val sceneMissing: Boolean,
    /** Last network failure message; surfaces in the header when
     *  set. Cleared on the next successful poll. */
    val networkError: String?,
    /** True while a rename is in flight; the optimistic
     *  capture-name update happens immediately, this drives any
     *  spinner if we add one (currently unused, kept on the state
     *  for parity with the legacy implementation's tracking). */
    val renaming: Boolean,
    /** Absolute studio base URL — used to resolve scene.thumb_url /
     *  orbit_url for the hero preview. Null until the activity reads
     *  it from intent extras. */
    val baseUrl: String? = null,
) {
    companion object {
        val Initial: CaptureDetailUiState = CaptureDetailUiState(
            captureName = "",
            capture = null,
            scene = null,
            sceneMissing = false,
            networkError = null,
            renaming = false,
            baseUrl = null,
        )
    }
}

@Immutable
data class CaptureRenameDialog(
    val initialName: String,
)
