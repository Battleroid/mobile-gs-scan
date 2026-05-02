package dev.battleroid.mobilegsscan

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.google.ar.core.ArCoreApk
import dev.battleroid.mobilegsscan.databinding.ActivityCaptureBinding
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10

/**
 * The capture screen.
 *
 * As of the local-record-then-upload pivot, this activity is no
 * longer responsible for any network I/O. It records frames + poses
 * to a [Draft] directory on local storage during capture. The user
 * decides what to do with the draft on Finish — upload now, save
 * for later, or discard — none of which require the studio to be
 * reachable from the phone's current network.
 *
 * Lifecycle:
 *   1. Validate ARCore availability + Google Play Services for AR.
 *   2. Request camera permission.
 *   3. Open the [Draft] passed in via [EXTRA_DRAFT_ID] (created by
 *      [MainActivity] before launching us). Bail with a toast if it
 *      no longer exists on disk.
 *   4. Render: every frame, advance ARCore, paint the camera quad
 *      via BackgroundRenderer, then draw the active overlay
 *      ([CoverageRenderer] for Phase 1 surface points, or
 *      [DepthMeshRenderer] for the Phase 3 depth-mesh prototype).
 *      Which one is picked from [ServerConfig.overlayStyle] at
 *      activity-create time and held immutable for the session.
 *      Depth mesh on a non-supporting device falls back to points
 *      without warning.
 *   5. Recording gate: frame writes are OFF until the user taps
 *      "Start capture". Until then the preview runs, the overlay
 *      accumulates state (so the user can see ARCore tracking the
 *      scene), but we don't commit anything to disk.
 *      Tapping Start flips the captureGateActive flag.
 *   6. On Finish: show a three-way dialog —
 *        - Upload now → finalize draft, route to [DraftDetailActivity]
 *          with auto-upload, which performs the WS replay and on
 *          success deletes the local copy + routes to the
 *          server-side capture detail.
 *        - Save for later → finalize draft, route home; the draft
 *          shows up in the home-screen drafts list.
 *        - Discard → delete the draft directory, route home.
 *
 * Capture rate, JPEG quality, overlay-alpha, and overlay-style
 * settings are read once at activity start from ServerConfig, same
 * as before.
 */
class CaptureActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_DRAFT_ID = "draft_id"
        private const val PERM_REQ = 0xC4
        private const val PLAY_SERVICES_FOR_AR_PKG = "com.google.ar.core"
        // Baseline top margin for the three top-aligned HUD
        // TextViews. Mirrors the layout_marginTop / layout_margin
        // 20dp value in activity_capture.xml; we add the systemBars
        // top inset on top so they don't slide under the status bar.
        private const val HUD_BASE_TOP_DP = 20
    }

    private lateinit var binding: ActivityCaptureBinding
    private var arSession: ARCaptureSession? = null
    private val background = BackgroundRenderer()

    // Phase 1 vs Phase 3 overlays. Exactly one is non-null per
    // session, picked at onCreate time from ServerConfig +
    // (eventually) the device's depth-support flag.
    private var coverage: CoverageRenderer? = null
    private var depthMesh: DepthMeshRenderer? = null
    private var resolvedStyle: ServerConfig.OverlayStyle = ServerConfig.OverlayStyle.POINTS

    private var baseUrl: String = ""
    private var draftId: String = ""
    private var draft: Draft? = null

    private var overlayAlpha: Float = 0.7f
    private var hudThrottleCounter = 0

    private var userRequestedArInstall = false

    @Volatile private var captureGateActive = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCaptureBinding.inflate(layoutInflater)
        setContentView(binding.root)

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
        // Pick the overlay renderer from settings. Whether the
        // device actually supports DEPTH_MESH (depth-mode-supported)
        // is checked once arSession comes up; we may downgrade to
        // POINTS at that point. For now, instantiate based on the
        // user's preference and we'll swap if needed.
        resolvedStyle = ServerConfig.overlayStyle(this)
        when (resolvedStyle) {
            ServerConfig.OverlayStyle.POINTS -> {
                coverage = CoverageRenderer().also { it.setAlpha(overlayAlpha) }
            }
            ServerConfig.OverlayStyle.DEPTH_MESH -> {
                depthMesh = DepthMeshRenderer().also { it.setAlpha(overlayAlpha) }
            }
        }

        binding.btnFinish.setOnClickListener { onFinishTapped() }
        binding.btnStart.setOnClickListener { onStartCaptureTapped() }
        // Local-record means we no longer need the server to be up
        // before the user can record. Enable Start as soon as the
        // ARCore session is wired up.
        binding.btnStart.isEnabled = true
        binding.frameCounter.text = getString(R.string.capture_idle)
        binding.coverageHud.text = getString(R.string.coverage_initial)
        binding.sessionLabel.text = draft?.meta?.name ?: ""

        binding.glSurface.setEGLContextClientVersion(2)
        binding.glSurface.setRenderer(Renderer())
        binding.glSurface.renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY

        applyHudInsets()

        ensurePermissionsThenConnect()
    }

    private fun applyHudInsets() {
        val baseTopPx = (HUD_BASE_TOP_DP * resources.displayMetrics.density).toInt()
        val topAnchored = listOf(
            binding.sessionLabel,
            binding.frameCounter,
            binding.coverageHud,
        )
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { _, insets ->
            val sys = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            val cutout = insets.getInsets(WindowInsetsCompat.Type.displayCutout())
            val topInset = maxOf(sys.top, cutout.top)
            topAnchored.forEach { v ->
                val lp = v.layoutParams as? ViewGroup.MarginLayoutParams
                    ?: return@forEach
                lp.topMargin = baseTopPx + topInset
                v.layoutParams = lp
            }
            insets
        }
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
            Toast.makeText(this, "ARCore resume failed: ${e.message}", Toast.LENGTH_LONG).show()
            finish()
            return
        }
        binding.glSurface.onResume()
    }

    override fun onPause() {
        binding.glSurface.onPause()
        arSession?.pause()
        super.onPause()
    }

    override fun onDestroy() {
        arSession?.close()
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERM_REQ) {
            if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
                bootstrapAr()
            } else {
                Toast.makeText(this, "camera denied", Toast.LENGTH_SHORT).show()
                finish()
            }
        }
    }

    private fun ensurePermissionsThenConnect() {
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.CAMERA), PERM_REQ,
            )
        } else {
            bootstrapAr()
        }
    }

    private fun bootstrapAr() {
        val avail = ArCoreApk.getInstance().checkAvailability(this)
        if (avail.isTransient) {
            binding.glSurface.postDelayed({ bootstrapAr() }, 200)
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
        val msg = buildString {
            append(getString(R.string.arcore_unsupported_body))
            if (!extra.isNullOrBlank()) {
                append("\n\n(")
                append(extra)
                append(")")
            }
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.arcore_unsupported_title)
            .setMessage(msg)
            .setCancelable(false)
            .setPositiveButton(R.string.action_open_play_store) { _, _ ->
                openPlayServicesForArInPlayStore()
                finish()
            }
            .setNegativeButton(R.string.action_cancel) { _, _ -> finish() }
            .show()
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
            arSession = ARCaptureSession(
                context = this,
                targetIntervalMs = ServerConfig.captureIntervalMs(this),
                jpegQuality = ServerConfig.captureJpegQuality(this),
            )
        } catch (e: Exception) {
            Toast.makeText(this, "ARCore session failed: ${e.message}", Toast.LENGTH_LONG).show()
            finish()
            return
        }
        if (background.textureId >= 0) {
            arSession?.setTextureName(background.textureId)
        }
        // Downgrade DEPTH_MESH → POINTS if the device doesn't
        // actually support depth (best-effort: the user's choice
        // was honoured if the hardware can do it; otherwise we
        // fall back silently). Leave the alpha alone — same value
        // applies regardless of which renderer is active.
        if (resolvedStyle == ServerConfig.OverlayStyle.DEPTH_MESH &&
            arSession?.depthSupported == false
        ) {
            depthMesh = null
            coverage = CoverageRenderer().also {
                // Created lazily after arSession exists — must
                // also init on the GL thread, deferred to the
                // renderer.
                it.setAlpha(overlayAlpha)
            }
            resolvedStyle = ServerConfig.OverlayStyle.POINTS
            // The Renderer.onSurfaceCreated path has already run
            // by the time we get here in some flows; hint it to
            // re-init the new renderer next frame via a flag.
            needsCoverageGlInit = true
        }
    }

    @Volatile private var needsCoverageGlInit = false

    private fun onStartCaptureTapped() {
        if (captureGateActive) return
        captureGateActive = true
        binding.startHint.visibility = View.GONE
        binding.btnStart.visibility = View.GONE
        binding.btnFinish.visibility = View.VISIBLE
        binding.frameCounter.text = "0 frames"
    }

    private fun onFinishTapped() {
        if (!captureGateActive) {
            // Never started — discard the empty draft and back out.
            draft?.delete()
            finish()
            return
        }
        captureGateActive = false
        val d = draft ?: run {
            finish()
            return
        }
        // Show three-way decision: upload now / save for later /
        // discard. We finalize-then-route in upload-now and
        // save-for-later; discard deletes the directory outright.
        AlertDialog.Builder(this)
            .setTitle(R.string.finish_dialog_title)
            .setMessage(
                getString(
                    R.string.finish_dialog_body_fmt,
                    d.meta.frame_count,
                ),
            )
            .setCancelable(false)
            .setPositiveButton(R.string.finish_action_upload_now) { _, _ ->
                d.finalize()
                routeToDraftDetail(d, autoUpload = true)
            }
            .setNeutralButton(R.string.finish_action_save_later) { _, _ ->
                d.finalize()
                routeHome()
            }
            .setNegativeButton(R.string.finish_action_discard) { _, _ ->
                d.delete()
                routeHome()
            }
            .show()
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

    /**
     * HUD update throttled to ~2 Hz so we don't UI-thread-thrash.
     * Format depends on the active overlay: Phase 1 shows the
     * coverage % + total points; Phase 3 prototype shows the
     * latest mesh's triangle count.
     */
    private fun maybeUpdateHud() {
        hudThrottleCounter++
        if (hudThrottleCounter % 4 != 0) return
        val text = when (resolvedStyle) {
            ServerConfig.OverlayStyle.POINTS -> {
                val stats = coverage?.coverageStats() ?: return
                getString(R.string.coverage_status_fmt, stats.wellCoveredPct, stats.totalPoints)
            }
            ServerConfig.OverlayStyle.DEPTH_MESH -> {
                val stats = depthMesh?.stats() ?: return
                getString(R.string.mesh_status_fmt, stats.triangles)
            }
        }
        runOnUiThread { binding.coverageHud.text = text }
    }

    private fun maybeUpdateFrameCounter() {
        val count = draft?.meta?.frame_count ?: return
        runOnUiThread {
            binding.frameCounter.text = "$count frames"
        }
    }

    private inner class Renderer : GLSurfaceView.Renderer {
        override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
            GLES20.glClearColor(0f, 0f, 0f, 1f)
            background.createOnGlThread()
            coverage?.createOnGlThread()
            depthMesh?.createOnGlThread()
            // Push the alpha after createOnGlThread so the first
            // frame already reflects the user's preference. setAlpha
            // is thread-safe; the call from onCreate above is the
            // source of truth, this just makes the GL-side state
            // consistent regardless of which path assigned the
            // field first.
            coverage?.setAlpha(overlayAlpha)
            depthMesh?.setAlpha(overlayAlpha)
            arSession?.setTextureName(background.textureId)
        }

        override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
            GLES20.glViewport(0, 0, width, height)
            arSession?.setDisplayGeometry(windowManager.defaultDisplay.rotation, width, height)
        }

        override fun onDrawFrame(gl: GL10?) {
            GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
            val ar = arSession ?: return
            val frame = ar.update() ?: return

            // Defensive: if startArSession swapped depthMesh →
            // coverage after onSurfaceCreated already ran, we need
            // to lazy-init the new renderer's GL resources here
            // (still on the GL thread).
            if (needsCoverageGlInit) {
                coverage?.createOnGlThread()
                coverage?.setAlpha(overlayAlpha)
                needsCoverageGlInit = false
            }

            background.updateTexCoords(frame)
            background.draw()

            val view = ar.viewMatrix(frame)
            val proj = ar.projectionMatrix(frame)

            // Phase 1 path: render the feature-point overlay on
            // every frame, accumulate observations only during
            // capture. Cheap.
            coverage?.let { c ->
                c.draw(view, proj)
                if (captureGateActive) {
                    try {
                        val pc = ar.acquirePointCloud(frame)
                        try { c.recordObservations(pc) } finally { pc.close() }
                    } catch (_: Exception) {
                        // Don't let a transient point-cloud failure
                        // kill recording; the splat trains from
                        // JPEGs + poses, the overlay is purely UX.
                    }
                }
            }

            // Phase 3 path: rebuild the mesh from the latest depth
            // image and draw it. The depth image isn't always ready
            // (NotYet on first few frames); when it isn't, we just
            // skip the rebuild and last-known mesh keeps showing.
            depthMesh?.let { m ->
                try {
                    val depthImage = ar.acquireDepthImage16(frame)
                    if (depthImage != null) {
                        try {
                            val intr = ar.colorIntrinsics(frame)
                            val pose = ar.cameraPose(frame)
                            m.update(depthImage, intr, pose)
                        } finally {
                            depthImage.close()
                        }
                    }
                } catch (_: Exception) {
                    // Same defensive policy: depth failures are
                    // overlay-only, don't kill recording.
                }
                m.draw(view, proj)
            }

            if (!captureGateActive) return
            val captured = ar.pollFrameData(frame) ?: return

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
            maybeUpdateHud()
            maybeUpdateFrameCounter()
        }
    }
}
