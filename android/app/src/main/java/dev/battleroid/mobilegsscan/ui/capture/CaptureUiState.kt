package dev.battleroid.mobilegsscan.ui.capture

import androidx.compose.runtime.Immutable
import dev.battleroid.mobilegsscan.quality.DropCounts
import dev.battleroid.mobilegsscan.quality.FrameQualityFilter

/**
 * Snapshot the [CaptureScreen] composable renders from. Driven
 * by [dev.battleroid.mobilegsscan.CaptureActivity] from a
 * [kotlinx.coroutines.flow.MutableStateFlow] — the GL renderer
 * pushes updates from the GL thread (StateFlow writes are
 * thread-safe, no `runOnUiThread` hop needed); the activity
 * pushes lifecycle changes (capture-gate flip, ARCore unsupported
 * dialog visibility) from the main thread.
 *
 * The bulky stuff (camera image, in-3D coverage dots) is rendered
 * by ``GLSurfaceView`` underneath the Compose overlay, not from
 * this state — Compose only paints chrome.
 */
@Immutable
data class CaptureUiState(
    /** Capture / draft display name. Shown in the top-left glass
     *  pill. Empty string while the draft hasn't resolved yet. */
    val sessionName: String,

    /** True once the user taps Start. Until then, the start hint
     *  shows in the centre and the bottom button reads "START".
     *  After flip, the start hint disappears and the button reads
     *  "FINISH". */
    val captureActive: Boolean,

    /** Live frame count from `Draft.meta.frame_count`. Renderer
     *  pushes this on every successful frame write — Compose
     *  diff-renders the top-right pill. */
    val frameCount: Int,

    /** Surface coverage stats from `CoverageRenderer.coverageStats`,
     *  throttled to ~2 Hz on the GL thread. Drives the centre-top
     *  ring + percentage. `null` until the first sample arrives. */
    val coverage: Coverage?,

    /** Cumulative drop counts from the on-device frame-quality
     *  filter. Drives the small chip next to the frame-count pill
     *  and the "Hold steady" banner. */
    val dropCounts: DropCounts = DropCounts(),

    /** Effective frames-per-second derived from accepted frames
     *  / capture-active duration. Updated on the GL thread at the
     *  same cadence as ``frameCount``. */
    val effectiveFps: Float = 0f,

    /** Set to a non-null reason when the filter has dropped
     *  ≥10 consecutive frames for the same user-actionable
     *  cause; CaptureScreen surfaces a transient banner. The
     *  activity clears this back to ``null`` after the next
     *  accept. */
    val streakWarning: FrameQualityFilter.DropReason? = null,
) {
    companion object {
        val Initial: CaptureUiState = CaptureUiState(
            sessionName = "",
            captureActive = false,
            frameCount = 0,
            coverage = null,
            dropCounts = DropCounts(),
            effectiveFps = 0f,
            streakWarning = null,
        )
    }
}

@Immutable
data class Coverage(
    /** Percentage 0-100 of tracked surface points the user has
     *  observed enough times to count as "well covered". Drives
     *  the ring's fill arc. */
    val wellCoveredPct: Int,
    /** Total tracked-points count, shown as the secondary line
     *  inside the coverage pill ("covered · 1 824 pts"). */
    val totalPoints: Int,
)

/**
 * Modal flag for the ARCore-unsupported dialog. Compose's
 * [androidx.compose.material3.AlertDialog] is driven by visibility
 * state (no AppCompat AlertDialog dependency this PR — keeps
 * CaptureActivity on `ComponentActivity`).
 */
@Immutable
data class CaptureDialogs(
    /** "ARCore reports unsupported" dialog visibility + message. */
    val arUnsupported: ArUnsupportedDialog?,
    /** Three-way "Finish capture" dialog. Shown when the user taps
     *  Finish on a non-empty capture. */
    val finishPrompt: FinishPrompt?,
) {
    companion object {
        val None: CaptureDialogs = CaptureDialogs(arUnsupported = null, finishPrompt = null)
    }
}

@Immutable
data class ArUnsupportedDialog(
    /** Optional extra detail (e.g. exception message) appended to
     *  the body. */
    val extra: String?,
)

@Immutable
data class FinishPrompt(
    /** Frame count to show in the dialog body ("Recorded N frames…"). */
    val frameCount: Int,
)
