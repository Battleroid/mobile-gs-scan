package dev.battleroid.mobilegsscan.ui.draft

import androidx.compose.runtime.Immutable
import dev.battleroid.mobilegsscan.DraftMeta
import java.io.File

/**
 * Snapshot the [DraftDetailScreen] composable renders from.
 * Driven by [dev.battleroid.mobilegsscan.DraftDetailActivity] —
 * the activity owns the upload coroutine and pushes progress
 * updates through this state.
 */
@Immutable
data class DraftDetailUiState(
    val meta: DraftMeta?,
    /** On-disk byte total — sum of JPEG sizes in the frames dir
     *  plus the poses.jsonl. Recomputed in [onResume] alongside
     *  the meta refresh. Surfaced as human-readable MB/KB in the
     *  title kicker and stats card so the user can sanity-check
     *  the size before tapping Upload now. */
    val totalBytes: Long,
    /** Non-null while an upload is in flight. Drives the
     *  collapse-into-progress-bar action row. */
    val upload: UploadProgress?,
    /** Surfaced upload-error message; null when nothing to show.
     *  The legacy activity used an AlertDialog for this — the
     *  Compose port keeps a state field instead so the screen can
     *  drive a Compose Material3 dialog. */
    val uploadError: String?,
    /** First captured JPEG in the draft's frames dir, or null when
     *  the draft was created but never captured into. Refreshed
     *  alongside ``totalBytes`` in onResume. Used as the hero
     *  preview so the user sees what they actually scanned. */
    val thumbnailFile: File? = null,
) {
    companion object {
        val Initial: DraftDetailUiState = DraftDetailUiState(
            meta = null,
            totalBytes = 0L,
            upload = null,
            uploadError = null,
            thumbnailFile = null,
        )
    }
}

@Immutable
data class UploadProgress(
    val sent: Int,
    val total: Int,
)
