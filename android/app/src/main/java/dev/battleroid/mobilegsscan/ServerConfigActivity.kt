package dev.battleroid.mobilegsscan

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import dev.battleroid.mobilegsscan.ui.settings.CameraProbeStatus
import dev.battleroid.mobilegsscan.ui.settings.SettingsScreen
import dev.battleroid.mobilegsscan.ui.settings.SettingsUiState
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Settings screen — Compose port of the prior programmatic
 * [ServerConfigActivity] form (no XML layout previously; it built
 * a LinearLayout of TextViews / EditTexts / SeekBars / Buttons
 * inline in onCreate).
 *
 * Fields surfaced (one save commits all of them):
 *   * Studio URL — text input, ``ServerConfig.setStudioUrl`` prepends
 *     the scheme on save when the user typed a bare host.
 *   * Capture format — chip row of ARCore CameraConfigs the device
 *     actually exposes, queried lazily on screen open. A Custom
 *     chip reveals the freeform fps slider below.
 *   * Capture FPS — slider, only rendered when the Custom format
 *     chip is selected.
 *   * JPEG quality — slider.
 *   * Training fidelity preset — Low / Standard / High / Custom.
 *   * Coverage overlay opacity — slider.
 *
 * Camera-config probe: a one-shot ARCore ``Session`` is created on
 * a background coroutine to enumerate ``getSupportedCameraConfigs``.
 * The session is closed immediately after — we don't keep it
 * around. Requires camera permission; absent permission, the probe
 * surfaces a soft inline warning + the user still gets the Custom
 * + freeform fps slider path.
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
            cameraConfigKey = ServerConfig.cameraConfigKey(this),
            cameraConfigs = emptyList(),
            cameraProbeStatus = CameraProbeStatus.Pending,
            captureProfile = ServerConfig.captureProfile(this),
            frameFilterEnabled = ServerConfig.frameFilterEnabled(this),
            frameFilterBlur = ServerConfig.frameFilterBlur(this),
            frameFilterMotion = ServerConfig.frameFilterMotion(this),
            frameFilterExposure = ServerConfig.frameFilterExposure(this),
        )

        runCameraConfigProbe()

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                SettingsScreen(
                    state = current,
                    onStudioUrlChange = { v -> state.update { it.copy(studioUrl = v) } },
                    onCaptureFpsChange = { v ->
                        // Editing fps via the slider drops the
                        // user out of any named profile — the
                        // values no longer match a canonical
                        // triple.
                        state.update { it.copy(captureFps = v, captureProfile = ServerConfig.CAPTURE_PROFILE_CUSTOM) }
                    },
                    onJpegQualityChange = { v -> state.update { it.copy(jpegQuality = v) } },
                    onTrainItersChange = { v -> state.update { it.copy(trainIters = v) } },
                    onOverlayAlphaPctChange = { v ->
                        state.update { it.copy(overlayAlphaPct = v) }
                    },
                    onCameraConfigKeyChange = { v ->
                        state.update { it.copy(cameraConfigKey = v) }
                    },
                    onCaptureProfileChange = ::onCaptureProfileChange,
                    onFrameFilterEnabledChange = { v ->
                        state.update { it.copy(frameFilterEnabled = v) }
                    },
                    onFrameFilterBlurChange = { v ->
                        state.update { it.copy(frameFilterBlur = v, captureProfile = ServerConfig.CAPTURE_PROFILE_CUSTOM) }
                    },
                    onFrameFilterMotionChange = { v ->
                        state.update { it.copy(frameFilterMotion = v, captureProfile = ServerConfig.CAPTURE_PROFILE_CUSTOM) }
                    },
                    onFrameFilterExposureChange = { v ->
                        state.update { it.copy(frameFilterExposure = v, captureProfile = ServerConfig.CAPTURE_PROFILE_CUSTOM) }
                    },
                    onSaveClick = ::onSave,
                    onBackClick = { finish() },
                    onProfileClick = ::openProfile,
                )
            }
        }
    }

    /**
     * One-shot ARCore probe. Skips entirely when camera permission
     * isn't granted yet so we don't trigger the permission prompt
     * from a settings screen; the user will go through the normal
     * AR capture flow first which already has a proper rationale
     * UI. On failure (no permission, ARCore not installed, no back
     * camera) the chip row falls back to the Custom-only state and
     * the freeform fps slider stays visible.
     */
    private fun runCameraConfigProbe() {
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            state.update {
                it.copy(
                    cameraProbeStatus = CameraProbeStatus.Failed(
                        "Grant camera access in an AR capture to see device-supported formats.",
                    ),
                )
            }
            return
        }
        lifecycleScope.launch {
            val configs = try {
                probeCameraConfigs(this@ServerConfigActivity)
            } catch (e: Exception) {
                state.update {
                    it.copy(
                        cameraProbeStatus = CameraProbeStatus.Failed(
                            "Couldn't read device formats: ${e.message ?: "unknown"}",
                        ),
                    )
                }
                return@launch
            }
            state.update { prev ->
                // A previously saved key may no longer exist in the
                // device's supported set (OS upgrade, ARCore version
                // change, different filter outcome). Without this
                // reset the chip row would show nothing selected
                // *and* the freeform fps slider would still be
                // hidden (the visibility gate is "key == CUSTOM"),
                // leaving the user with no visible control. Normalize
                // stale keys to Custom so the slider reappears.
                val normalizedKey =
                    if (prev.cameraConfigKey == ServerConfig.CAMERA_CONFIG_CUSTOM ||
                        configs.any { it.key == prev.cameraConfigKey }
                    ) {
                        prev.cameraConfigKey
                    } else {
                        ServerConfig.CAMERA_CONFIG_CUSTOM
                    }
                prev.copy(
                    cameraConfigKey = normalizedKey,
                    cameraConfigs = configs,
                    cameraProbeStatus = CameraProbeStatus.Ok,
                )
            }
        }
    }

    private fun openProfile() {
        startActivity(android.content.Intent(this, ProfileActivity::class.java))
    }

    /** Selecting a named profile snaps fps + filter values to the
     *  canonical triple for that profile. Custom leaves the
     *  underlying sliders alone — useful when the user wants to
     *  hand-tune from a starting point. */
    private fun onCaptureProfileChange(profile: String) {
        state.update { prev ->
            val updated = when (profile) {
                ServerConfig.CAPTURE_PROFILE_SMOOTH -> prev.copy(
                    captureProfile = profile,
                    captureFps = 30,
                    frameFilterEnabled = true,
                    frameFilterBlur = 60,    // lenient
                    frameFilterMotion = 30,  // lenient
                    frameFilterExposure = 30,
                )
                ServerConfig.CAPTURE_PROFILE_BALANCED -> prev.copy(
                    captureProfile = profile,
                    captureFps = 15,
                    frameFilterEnabled = true,
                    frameFilterBlur = ServerConfig.DEFAULT_FRAME_FILTER_BLUR,
                    frameFilterMotion = ServerConfig.DEFAULT_FRAME_FILTER_MOTION,
                    frameFilterExposure = ServerConfig.DEFAULT_FRAME_FILTER_EXPOSURE,
                )
                ServerConfig.CAPTURE_PROFILE_SPARSE -> prev.copy(
                    captureProfile = profile,
                    captureFps = 8,
                    frameFilterEnabled = true,
                    frameFilterBlur = 140,   // strict
                    frameFilterMotion = 75,  // strict
                    frameFilterExposure = 70,
                )
                else -> prev.copy(captureProfile = ServerConfig.CAPTURE_PROFILE_CUSTOM)
            }
            updated
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
        ServerConfig.setCameraConfigKey(this, s.cameraConfigKey)
        ServerConfig.setCaptureProfile(this, s.captureProfile)
        ServerConfig.setFrameFilterEnabled(this, s.frameFilterEnabled)
        ServerConfig.setFrameFilterBlur(this, s.frameFilterBlur)
        ServerConfig.setFrameFilterMotion(this, s.frameFilterMotion)
        ServerConfig.setFrameFilterExposure(this, s.frameFilterExposure)
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
