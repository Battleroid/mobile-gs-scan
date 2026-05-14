package dev.battleroid.mobilegsscan

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import com.google.ar.core.ArCoreApk
import dev.battleroid.mobilegsscan.ui.capture.ArUnsupportedDialog
import dev.battleroid.mobilegsscan.ui.capture.CaptureDialogs
import dev.battleroid.mobilegsscan.ui.capture.CaptureScreen
import dev.battleroid.mobilegsscan.ui.capture.CaptureUiState
import dev.battleroid.mobilegsscan.ui.capture.Coverage
import dev.battleroid.mobilegsscan.ui.capture.FinishPrompt
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * The capture screen.
 *
 * Compose port of the prior XML-driven capture activity. The
 * ARCore + camera + frame writer machinery (ARCaptureSession,
 * BackgroundRenderer, CoverageRenderer, the GL renderer, draft
 * persistence, capture-gate state machine, three-way Finish flow)
 * is preserved verbatim — only the HUD chrome is rebuilt in
 * Compose. The GLSurfaceView is created here and handed to
 * [CaptureScreen] via an `AndroidView` factory; the HUD lives
 * above it in the same Box.
 *
 * Lifecycle (unchanged from the legacy implementation):
 *   1. Validate ARCore availability + Google Play Services for AR.
 *   2. Request camera permission.
 *   3. Open the [Draft] passed in via [EXTRA_DRAFT_ID]. Bail with
 *      a toast if it no longer exists on disk.
 *   4. Render: every frame, advance ARCore, paint the camera quad
 *      via BackgroundRenderer, then draw the [CoverageRenderer]
 *      overlay so the user sees Scaniverse-style colored dots on
 *      the actual surfaces showing how thoroughly each region has
 *      been captured.
 *   5. Recording gate: frame writes are OFF until the user taps
 *      Start. Until then the preview runs, the coverage overlay
 *      accumulates points, but we don't commit anything to disk.
 *      Tapping Start flips the captureGateActive flag.
 *   6. On Finish: show the three-way prompt (upload now / save
 *      for later / discard) and route accordingly.
 *
 * Notable Compose-vs-XML choices:
 *  - `ComponentActivity` instead of `AppCompatActivity`. The two
 *    AlertDialogs (ARCore unsupported, Finish prompt) are now
 *    Compose `AlertDialog`s driven by [CaptureDialogs] state;
 *    no AppCompat dependency needed.
 *  - GL renderer pushes HUD updates directly to the
 *    [MutableStateFlow] from the GL thread — StateFlow writes are
 *    thread-safe, so no `runOnUiThread` hop. Compose recomposes
 *    on the main thread when the flow emits.
 *  - GLSurfaceView is held as an activity property (not Compose
 *    state) because its lifecycle is tied to onResume/onPause and
 *    we don't want recomposition to recreate it.
 */
class CaptureActivity : ComponentActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_DRAFT_ID = "draft_id"
        private const val PLAY_SERVICES_FOR_AR_PKG = "com.google.ar.core"
    }

    // Modern permission-result API. ComponentActivity doesn't expose
    // `onRequestPermissionsResult` as overridable (that path lives on
    // AppCompatActivity); registerForActivityResult is the post-Compose
    // replacement and runs on the same callback thread the legacy
    // override did. Registered at activity-construction time so the
    // result handler survives configuration changes.
    private val cameraPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            bootstrapAr()
        } else {
            Toast.makeText(this, "camera denied", Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    private val state: MutableStateFlow<CaptureUiState> =
        MutableStateFlow(CaptureUiState.Initial)
    private val uiState: StateFlow<CaptureUiState> = state.asStateFlow()
    private val dialogs: MutableStateFlow<CaptureDialogs> =
        MutableStateFlow(CaptureDialogs.None)
    private val dialogState: StateFlow<CaptureDialogs> = dialogs.asStateFlow()

    private var arSession: ARCaptureSession? = null
    private val background = BackgroundRenderer()
    private val coverage = CoverageRenderer()
    private var glSurface: GLSurfaceView? = null

    private var baseUrl: String = ""
    private var draftId: String = ""
    private var draft: Draft? = null

    private var overlayAlpha: Float = 0.7f
    private var coverageHudCounter = 0

    private var userRequestedArInstall = false

    @Volatile private var captureGateActive = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        draftId = intent.getStringExtra(EXTRA_DRAFT_ID).orEmpty()
        if (draftId.isEmpty()) {
            Toast.makeText(this, "missing draft id", Toast.LENGTH_SHORT).show()
            finish()
            return
        }
        draft = DraftStore.openDraft(this, draftId)
        if (draft == null) {
            Toast.makeText(this, "draft no longer exists", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        overlayAlpha = ServerConfig.coverageOverlayAlphaFloat(this)
        coverage.setAlpha(overlayAlpha)

        state.update {
            it.copy(sessionName = draft?.meta?.name.orEmpty())
        }

        // Back-press handler: route through the same logic as
        // tapping FINISH. A user who hit "+ new" then backed out
        // without recording anything used to leave an empty draft
        // sitting on the home screen; mirror onFinishTapped so the
        // gesture cleans up after itself. With frames recorded,
        // back surfaces the three-way Finish prompt — same as
        // tapping the CTA — so the user doesn't accidentally lose
        // work to a stray gesture.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                onFinishTapped()
            }
        })

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                val live by dialogState.collectAsState()
                CaptureScreen(
                    state = current,
                    dialogs = live,
                    onStartClick = ::onStartCaptureTapped,
                    onFinishClick = ::onFinishTapped,
                    onArUnsupportedConfirm = ::onArUnsupportedConfirm,
                    onArUnsupportedDismiss = ::onArUnsupportedDismiss,
                    onFinishUploadNow = ::onFinishUploadNow,
                    onFinishSaveLater = ::onFinishSaveLater,
                    onFinishDiscard = ::onFinishDiscard,
                    glSurfaceFactory = ::createGlSurface,
                )
            }
        }

        ensurePermissionsThenConnect()
    }

    /**
     * One-shot factory for [AndroidView]. Caches the constructed
     * [GLSurfaceView] on the activity so onResume/onPause can drive
     * its lifecycle directly without going through Compose state.
     * Compose only calls this once per `AndroidView` mount; the
     * cached check is defensive against any future re-mount.
     *
     * Lifecycle race: unlike `setContentView`, Compose's
     * `AndroidView` factory runs during composition, which is
     * dispatched onto the main thread and can land *after*
     * `onResume()` has already executed. If the activity's
     * `onResume` ran before this factory, the `glSurface?.onResume()`
     * call there short-circuited on null and the surface starts
     * stuck — rendering paused/blank until the user backgrounds
     * and reopens the activity. Catch that here by syncing the
     * new view to the current lifecycle state on creation.
     */
    private fun createGlSurface(context: android.content.Context): GLSurfaceView {
        glSurface?.let { return it }
        val view = GLSurfaceView(context).apply {
            setEGLContextClientVersion(2)
            setRenderer(Renderer())
            renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
        }
        glSurface = view
        if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
            view.onResume()
        }
        return view
    }

    override fun onResume() {
        super.onResume()
        if (arSession == null && userRequestedArInstall) {
            bootstrapAr()
            return
        }
        try {
            arSession?.resume()
        } catch (e: Exception) {
            Toast.makeText(
                this,
                "ARCore resume failed: ${e.message}",
                Toast.LENGTH_LONG,
            ).show()
            finish()
            return
        }
        glSurface?.onResume()
    }

    override fun onPause() {
        glSurface?.onPause()
        arSession?.pause()
        super.onPause()
    }

    override fun onDestroy() {
        arSession?.close()
        super.onDestroy()
    }

    private fun ensurePermissionsThenConnect() {
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            cameraPermLauncher.launch(Manifest.permission.CAMERA)
        } else {
            bootstrapAr()
        }
    }

    private fun bootstrapAr() {
        val avail = ArCoreApk.getInstance().checkAvailability(this)
        if (avail.isTransient) {
            // Was previously `binding.glSurface.postDelayed`; the
            // view may not yet be inflated under Compose's lazy
            // mount, so use the main-thread Handler directly.
            Handler(Looper.getMainLooper()).postDelayed({ bootstrapAr() }, 200)
            return
        }
        when (avail) {
            ArCoreApk.Availability.SUPPORTED_INSTALLED -> startArSession()

            ArCoreApk.Availability.SUPPORTED_NOT_INSTALLED,
            ArCoreApk.Availability.SUPPORTED_APK_TOO_OLD -> {
                try {
                    val res = ArCoreApk.getInstance()
                        .requestInstall(this, !userRequestedArInstall)
                    when (res) {
                        ArCoreApk.InstallStatus.INSTALL_REQUESTED -> {
                            userRequestedArInstall = true
                        }
                        ArCoreApk.InstallStatus.INSTALLED -> startArSession()
                        null -> startArSession()
                    }
                } catch (e: Exception) {
                    showArUnsupportedDialog(e.message)
                }
            }

            ArCoreApk.Availability.UNSUPPORTED_DEVICE_NOT_CAPABLE -> {
                showArUnsupportedDialog(null)
            }

            else -> {
                Toast.makeText(
                    this,
                    "ARCore check failed (${avail.name})",
                    Toast.LENGTH_LONG,
                ).show()
                finish()
            }
        }
    }

    private fun showArUnsupportedDialog(extra: String?) {
        dialogs.update { it.copy(arUnsupported = ArUnsupportedDialog(extra = extra)) }
    }

    private fun onArUnsupportedConfirm() {
        dialogs.update { it.copy(arUnsupported = null) }
        openPlayServicesForArInPlayStore()
        finish()
    }

    private fun onArUnsupportedDismiss() {
        dialogs.update { it.copy(arUnsupported = null) }
        finish()
    }

    private fun openPlayServicesForArInPlayStore() {
        val marketIntent = Intent(
            Intent.ACTION_VIEW,
            Uri.parse("market://details?id=$PLAY_SERVICES_FOR_AR_PKG"),
        ).apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
        try {
            startActivity(marketIntent)
        } catch (_: Exception) {
            startActivity(
                Intent(
                    Intent.ACTION_VIEW,
                    Uri.parse(
                        "https://play.google.com/store/apps/details?id=$PLAY_SERVICES_FOR_AR_PKG",
                    ),
                ).apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) },
            )
        }
    }

    private fun startArSession() {
        try {
            // Always pass the slider-driven interval as the FALLBACK
            // throttle. ARCaptureSession decides internally whether
            // to honor it: when the preset key resolves to a real
            // device-supported CameraConfig, ARCore paces at the
            // hardware rate and the app-side throttle is disabled.
            // When the key is Custom OR stale (no matching config),
            // the fallback interval keeps us from flooding the wire
            // at ARCore's default rate.
            arSession = ARCaptureSession(
                context = this,
                customIntervalMs = ServerConfig.captureIntervalMs(this),
                jpegQuality = ServerConfig.captureJpegQuality(this),
                cameraConfigKey = ServerConfig.cameraConfigKey(this),
            )
        } catch (e: Exception) {
            Toast.makeText(
                this,
                "ARCore session failed: ${e.message}",
                Toast.LENGTH_LONG,
            ).show()
            finish()
            return
        }
        if (background.textureId >= 0) {
            arSession?.setTextureName(background.textureId)
        }
    }

    private fun onStartCaptureTapped() {
        if (captureGateActive) return
        captureGateActive = true
        state.update { it.copy(captureActive = true, frameCount = 0) }
    }

    private fun onFinishTapped() {
        val frames = draft?.meta?.frame_count ?: 0
        // No frames committed → discard. Single condition (not
        // gated on captureGateActive) so a second back press while
        // the Finish prompt is already up doesn't silently delete
        // a draft that has frames in it. The gate-active check
        // would invert on re-entry — first call flips the gate to
        // false to stop recording, second call sees `!gate` and
        // would erase a non-empty draft.
        //
        // Covers both legacy discard cases by virtue of the frames
        // check alone:
        //   * user hit "+ new" and immediately backed out (gate
        //     never flipped, frames = 0)
        //   * user tapped Start but Finished / backed out before any
        //     frame landed on disk (gate flipped, frames = 0)
        if (frames == 0) {
            // Stop the GL thread before deleting — otherwise an
            // in-flight `onDrawFrame` that already passed its
            // `if (!captureGateActive) return` check can race into
            // `appendFrame` against a just-deleted draft directory
            // and trigger a `frame write failed` toast on a screen
            // the user has already backed out of. Flipping the gate
            // doesn't fully eliminate the race (a frame that's
            // already past the gate-check still completes), but the
            // appendFrame try/catch swallows that one exception
            // cleanly — and any subsequent draw call short-circuits.
            captureGateActive = false
            draft?.delete()
            finish()
            return
        }
        // Surface the three-way prompt; Finish is committed when
        // the user picks one of the three handlers. Idempotent on
        // re-entry — second back press while the dialog is up just
        // re-applies the same MutableStateFlow values.
        captureGateActive = false
        state.update { it.copy(captureActive = false) }
        dialogs.update {
            it.copy(finishPrompt = FinishPrompt(frameCount = frames))
        }
    }

    private fun onFinishUploadNow() {
        dialogs.update { it.copy(finishPrompt = null) }
        val d = draft ?: run { finish(); return }
        d.finalize()
        routeToDraftDetail(d, autoUpload = true)
    }

    private fun onFinishSaveLater() {
        dialogs.update { it.copy(finishPrompt = null) }
        val d = draft ?: run { finish(); return }
        d.finalize()
        routeHome()
    }

    private fun onFinishDiscard() {
        dialogs.update { it.copy(finishPrompt = null) }
        draft?.delete()
        routeHome()
    }

    private fun routeHome() {
        val home = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        startActivity(home)
        finish()
    }

    private fun routeToDraftDetail(d: Draft, autoUpload: Boolean) {
        val home = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val detail = Intent(this, DraftDetailActivity::class.java).apply {
            putExtra(DraftDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(DraftDetailActivity.EXTRA_DRAFT_ID, d.id)
            putExtra(DraftDetailActivity.EXTRA_AUTO_UPLOAD, autoUpload)
        }
        startActivities(arrayOf(home, detail))
        finish()
    }

    private fun maybeUpdateCoverageHud() {
        coverageHudCounter++
        if (coverageHudCounter % 4 != 0) return
        val stats = coverage.coverageStats()
        // StateFlow.value is thread-safe — push from the GL thread
        // directly. Compose's snapshot system handles the main-thread
        // recomposition.
        state.update {
            it.copy(
                coverage = Coverage(
                    wellCoveredPct = stats.wellCoveredPct,
                    totalPoints = stats.totalPoints,
                ),
            )
        }
    }

    private fun maybeUpdateFrameCounter() {
        val count = draft?.meta?.frame_count ?: return
        state.update { it.copy(frameCount = count) }
    }

    private inner class Renderer : GLSurfaceView.Renderer {
        override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
            GLES20.glClearColor(0f, 0f, 0f, 1f)
            background.createOnGlThread()
            coverage.createOnGlThread()
            coverage.setAlpha(overlayAlpha)
            arSession?.setTextureName(background.textureId)
        }

        override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
            GLES20.glViewport(0, 0, width, height)
            arSession?.setDisplayGeometry(
                windowManager.defaultDisplay.rotation,
                width,
                height,
            )
        }

        override fun onDrawFrame(gl: GL10?) {
            GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
            val ar = arSession ?: return
            val frame = ar.update() ?: return

            background.updateTexCoords(frame)
            background.draw()

            // Coverage overlay rides on top of the camera quad. Uses
            // ARCore's view + projection so the dots project onto
            // the surfaces ARCore is tracking, not into thin air.
            coverage.draw(ar.viewMatrix(frame), ar.projectionMatrix(frame))

            if (!captureGateActive) return
            val captured = ar.pollFrameData(frame) ?: return

            // Record this frame's tracked feature points into the
            // overlay for visual feedback. PointCloud is Closeable;
            // wrap so we always release ARCore's hold.
            try {
                val pc = ar.acquirePointCloud(frame)
                try {
                    coverage.recordObservations(pc)
                } finally {
                    pc.close()
                }
            } catch (_: Exception) {
                // Don't let a transient point-cloud failure kill
                // recording; the splat trains from JPEGs + poses,
                // the overlay is purely a UX layer.
            }

            // Persist the frame to the draft directory. This runs on
            // the GL thread which is fine for the volume we deal
            // with (10 fps × ~150 KB JPEG = 1.5 MB/s). If we ever
            // start dropping frames here we'd push the disk write
            // onto a single-threaded coroutine.
            val d = draft ?: return
            try {
                d.appendFrame(
                    idx = captured.idx,
                    jpeg = captured.jpeg,
                    pose = captured.pose,
                    intrinsics = captured.intrinsics,
                )
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(
                        this@CaptureActivity,
                        "frame write failed: ${e.message}",
                        Toast.LENGTH_SHORT,
                    ).show()
                }
                return
            }
            maybeUpdateCoverageHud()
            maybeUpdateFrameCounter()
        }
    }
}
