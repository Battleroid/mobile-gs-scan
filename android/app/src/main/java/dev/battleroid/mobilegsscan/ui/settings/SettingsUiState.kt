package dev.battleroid.mobilegsscan.ui.settings

import androidx.compose.runtime.Immutable
import dev.battleroid.mobilegsscan.CameraConfigOption
import dev.battleroid.mobilegsscan.ServerConfig

/**
 * Snapshot the [SettingsScreen] composable renders from. Mirrors
 * [dev.battleroid.mobilegsscan.ServerConfig] one-to-one: the
 * Compose layer never reads the SharedPreferences directly, only
 * this snapshot. The hosting activity reads ServerConfig once at
 * onCreate to seed [HomeUiState], then commits the user's changes
 * back via `onSave` (which writes via the ServerConfig setters
 * and persists the values).
 */
@Immutable
data class SettingsUiState(
    val studioUrl: String,
    val captureFps: Int,
    val jpegQuality: Int,
    val trainIters: Int,
    val overlayAlphaPct: Int,
    /** Selected ARCore CameraConfig preset id, e.g.
     *  ``1920x1080@30`` or [ServerConfig.CAMERA_CONFIG_CUSTOM] when
     *  the user wants the freeform fps slider instead. */
    val cameraConfigKey: String,
    /** Device-supported preset list. Populated by the activity
     *  after the one-shot ARCore probe completes; empty while
     *  pending or after a probe failure (see [cameraProbeStatus]). */
    val cameraConfigs: List<CameraConfigOption>,
    /** Probe lifecycle for the Capture format section. ``Pending``
     *  during the initial ARCore session probe; ``Ok`` once the
     *  list is in hand (may still be empty on devices with no
     *  supported configs); ``Failed`` carries a user-facing reason
     *  ("Grant camera access first", "ARCore not installed", …). */
    val cameraProbeStatus: CameraProbeStatus,
) {
    companion object {
        /** Empty seed; the activity overwrites every field from
         *  ServerConfig in onCreate before the screen renders. Values
         *  here are intentionally `MIN_*` rather than real defaults
         *  so a renderer mistakenly reading this snapshot pre-seed
         *  surfaces as visibly broken (slider pinned to its minimum)
         *  rather than a plausible-looking lie. */
        val Initial: SettingsUiState = SettingsUiState(
            studioUrl = "",
            captureFps = ServerConfig.MIN_FPS,
            jpegQuality = ServerConfig.MIN_JPEG_QUALITY,
            trainIters = ServerConfig.DEFAULT_TRAIN_ITERS,
            overlayAlphaPct = ServerConfig.MIN_OVERLAY_ALPHA_PCT,
            cameraConfigKey = ServerConfig.CAMERA_CONFIG_CUSTOM,
            cameraConfigs = emptyList(),
            cameraProbeStatus = CameraProbeStatus.Pending,
        )
    }
}

sealed interface CameraProbeStatus {
    data object Pending : CameraProbeStatus
    data object Ok : CameraProbeStatus
    /** Probe failed — usually because camera permission isn't
     *  granted yet (Settings is reachable before any AR capture).
     *  ``reason`` is rendered as a soft inline note above the
     *  legacy fps slider so the user understands why the chip row
     *  isn't there. */
    data class Failed(val reason: String) : CameraProbeStatus
}
