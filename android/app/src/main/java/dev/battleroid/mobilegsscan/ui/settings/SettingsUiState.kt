package dev.battleroid.mobilegsscan.ui.settings

import androidx.compose.runtime.Immutable
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
        )
    }
}
