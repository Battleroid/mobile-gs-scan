package dev.battleroid.mobilegsscan

import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import dev.battleroid.mobilegsscan.ui.settings.SettingsScreen
import dev.battleroid.mobilegsscan.ui.settings.SettingsUiState
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * Settings screen — Compose port of the prior programmatic
 * [ServerConfigActivity] form (no XML layout previously; it built
 * a LinearLayout of TextViews / EditTexts / SeekBars / Buttons
 * inline in onCreate).
 *
 * Same five fields the legacy form surfaced:
 *   * Studio URL (text input, scheme prepended on save by
 *     [ServerConfig.setStudioUrl]).
 *   * Capture FPS (slider, range from [ServerConfig.MIN_FPS] to
 *     [ServerConfig.MAX_FPS]).
 *   * JPEG quality (slider, [ServerConfig.MIN_JPEG_QUALITY]..
 *     [ServerConfig.MAX_JPEG_QUALITY]).
 *   * Training fidelity preset (3-button row: Low / Standard /
 *     High, mapped to [ServerConfig.TRAIN_ITERS_LOW] / `_STANDARD`
 *     / `_HIGH`).
 *   * Coverage overlay opacity (slider,
 *     [ServerConfig.MIN_OVERLAY_ALPHA_PCT]..`MAX_OVERLAY_ALPHA_PCT`).
 *
 * Behaviour preserved verbatim — Save commits all five fields
 * back to [ServerConfig] and finishes the activity (returning to
 * the home screen, which re-reads ServerConfig on its next
 * onResume tick). URL-empty toast still surfaces on save attempt.
 */
class ServerConfigActivity : ComponentActivity() {
    private val state: MutableStateFlow<SettingsUiState> =
        MutableStateFlow(SettingsUiState.Initial)
    private val uiState: StateFlow<SettingsUiState> = state.asStateFlow()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        // Seed the form from persisted settings. The legacy
        // implementation read each field individually at the point
        // each widget was built; doing it in one shot here keeps
        // the rest of the screen pure-render.
        state.value = SettingsUiState(
            studioUrl = ServerConfig.studioUrl(this).orEmpty(),
            captureFps = ServerConfig.captureFps(this),
            jpegQuality = ServerConfig.captureJpegQuality(this),
            trainIters = ServerConfig.captureTrainIters(this),
            overlayAlphaPct = ServerConfig.coverageOverlayAlphaPct(this),
        )

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                SettingsScreen(
                    state = current,
                    onStudioUrlChange = { v -> state.update { it.copy(studioUrl = v) } },
                    onCaptureFpsChange = { v -> state.update { it.copy(captureFps = v) } },
                    onJpegQualityChange = { v -> state.update { it.copy(jpegQuality = v) } },
                    onTrainItersChange = { v -> state.update { it.copy(trainIters = v) } },
                    onOverlayAlphaPctChange = { v ->
                        state.update { it.copy(overlayAlphaPct = v) }
                    },
                    onSaveClick = ::onSave,
                    onBackClick = { finish() },
                )
            }
        }
    }

    private fun onSave() {
        val s = state.value
        val url = s.studioUrl.trim()
        if (url.isEmpty()) {
            Toast.makeText(this, "studio URL required", Toast.LENGTH_SHORT).show()
            return
        }
        ServerConfig.setStudioUrl(this, url)
        ServerConfig.setCaptureFps(this, s.captureFps)
        ServerConfig.setCaptureJpegQuality(this, s.jpegQuality)
        ServerConfig.setCaptureTrainIters(this, s.trainIters)
        ServerConfig.setCoverageOverlayAlphaPct(this, s.overlayAlphaPct)
        val saved = ServerConfig.studioUrl(this).orEmpty()
        if (saved != url) {
            // setStudioUrl prepends https:// when no scheme was
            // given; surface that to the user so they don't think
            // their input was silently replaced.
            Toast.makeText(this, "saved as $saved", Toast.LENGTH_SHORT).show()
        }
        finish()
    }
}
