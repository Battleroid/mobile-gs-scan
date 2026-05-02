package dev.battleroid.mobilegsscan

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.ar.core.ArCoreApk
import dev.battleroid.mobilegsscan.databinding.ActivityCaptureBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10

/**
 * The capture screen.
 *
 * Lifecycle:
 *   1. Validate ARCore availability + Google Play Services for AR.
 *   2. Request camera permission.
 *   3. Resolve capture id + name (from extras for the phone-driven
 *      path, or via /api/captures/by-token for the legacy QR deep
 *      link path). Done in startArSession's coroutine; doesn't gate
 *      the GL preview.
 *   4. Render: every frame, advance ARCore, paint the camera quad
 *      via BackgroundRenderer so the user sees what they're aiming
 *      at. This loop runs from the moment the GL surface is ready,
 *      independent of whether the user has tapped Start.
 *   5. Streaming gate: frame ingestion is OFF until the user taps
 *      "Start capture". This lets them aim and frame the subject
 *      first. Tapping Start opens the WebSocket, sends the session
 *      header, kicks off the heartbeat loop, and flips the
 *      captureGateActive flag so subsequent draws extract +
 *      transmit JPEG/pose data.
 *   6. On Finish: send the WS finalize message, then route the
 *      user to the native CaptureDetailActivity (stacked on top of
 *      the home screen) so they land on the pipeline-progress view
 *      instead of the browser. CaptureDetailActivity polls
 *      /api/captures + /api/scenes for live job state.
 */
class CaptureActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_PAIR_TOKEN = "pair_token"
        const val EXTRA_CAPTURE_ID = "capture_id"
        const val EXTRA_CAPTURE_NAME = "capture_name"
        private const val PERM_REQ = 0xC4
        private const val PLAY_SERVICES_FOR_AR_PKG = "com.google.ar.core"
    }

    private lateinit var binding: ActivityCaptureBinding
    private var arSession: ARCaptureSession? = null
    private val background = BackgroundRenderer()
    private var streamer: StreamingClient? = null
    private var heartbeatJob: Job? = null

    private var baseUrl: String = ""
    private var pairToken: String = ""
    private var captureId: String? = null
    private var captureName: String? = null
    private var sceneId: String? = null

    private var receivedCount = 0
    private var droppedCount = 0

    private var userRequestedArInstall = false

    @Volatile private var captureGateActive = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCaptureBinding.inflate(layoutInflater)
        setContentView(binding.root)

        baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        pairToken = intent.getStringExtra(EXTRA_PAIR_TOKEN).orEmpty()
        if (baseUrl.isEmpty() || pairToken.isEmpty()) {
            Toast.makeText(this, "missing pair token", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        binding.btnFinish.setOnClickListener { finishCapture() }
        binding.btnStart.setOnClickListener { onStartCaptureTapped() }
        binding.btnStart.isEnabled = false
        binding.frameCounter.text = getString(R.string.capture_idle)

        binding.glSurface.setEGLContextClientVersion(2)
        binding.glSurface.setRenderer(Renderer())
        binding.glSurface.renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY

        ensurePermissionsThenConnect()
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
        heartbeatJob?.cancel()
        streamer?.close()
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
            arSession = ARCaptureSession(this)
        } catch (e: Exception) {
            Toast.makeText(this, "ARCore session failed: ${e.message}", Toast.LENGTH_LONG).show()
            finish()
            return
        }
        if (background.textureId >= 0) {
            arSession?.setTextureName(background.textureId)
        }

        val preCaptureId = intent.getStringExtra(EXTRA_CAPTURE_ID)
        val preCaptureName = intent.getStringExtra(EXTRA_CAPTURE_NAME).orEmpty()

        lifecycleScope.launch {
            val info: CaptureInfo? = if (!preCaptureId.isNullOrBlank()) {
                CaptureInfo(
                    captureId = preCaptureId,
                    captureName = preCaptureName.ifBlank { "phone capture" },
                )
            } else {
                withContext(Dispatchers.IO) { resolveCaptureFromToken() }
            }
            if (info == null) {
                Toast.makeText(this@CaptureActivity, "pair token invalid", Toast.LENGTH_LONG).show()
                finish()
                return@launch
            }
            captureId = info.captureId
            captureName = info.captureName
            binding.sessionLabel.text = info.captureName
            binding.btnStart.isEnabled = true
        }
    }

    private fun onStartCaptureTapped() {
        val cap = captureId
        if (cap.isNullOrBlank()) return
        if (captureGateActive) return

        streamer = StreamingClient(
            scope = lifecycleScope,
            baseUrl = baseUrl,
            captureId = cap,
            pairToken = pairToken,
        ).also { s ->
            s.connect()
            lifecycleScope.launch {
                s.events.collect(::handleStreamEvent)
            }
            s.sendSession(
                deviceLabel = android.os.Build.MODEL,
                intrinsics = Intrinsics(0f, 0f, 0f, 0f, 0, 0),
                hasPose = true,
            )
        }

        heartbeatJob = lifecycleScope.launch {
            while (true) {
                kotlinx.coroutines.delay(5_000)
                streamer?.heartbeat()
            }
        }

        captureGateActive = true
        binding.startHint.visibility = View.GONE
        binding.btnStart.visibility = View.GONE
        binding.btnFinish.visibility = View.VISIBLE
        binding.frameCounter.text = "0 frames"
    }

    private data class CaptureInfo(val captureId: String, val captureName: String)

    private fun resolveCaptureFromToken(): CaptureInfo? {
        val client = OkHttpClient()
        val req = Request.Builder()
            .url("$baseUrl/api/captures/by-token/$pairToken")
            .build()
        return runCatching {
            client.newCall(req).execute().use { res ->
                if (!res.isSuccessful) return null
                val body = res.body?.string().orEmpty()
                val obj = JSONObject(body)
                CaptureInfo(
                    captureId = obj.getString("id"),
                    captureName = obj.optString("name", "capture"),
                )
            }
        }.getOrNull()
    }

    private fun handleStreamEvent(evt: StreamingClient.Event) {
        when (evt) {
            is StreamingClient.Event.Ack -> {
                receivedCount = evt.received
                droppedCount = evt.dropped
                runOnUiThread {
                    binding.frameCounter.text = if (droppedCount > 0) {
                        "$receivedCount frames ($droppedCount dropped)"
                    } else {
                        "$receivedCount frames"
                    }
                }
            }
            is StreamingClient.Event.Limit -> {
                runOnUiThread {
                    Toast.makeText(this, "server cap: ${evt.reason}", Toast.LENGTH_SHORT).show()
                }
            }
            is StreamingClient.Event.Queued -> {
                sceneId = evt.sceneId
                routeToCaptureDetail()
            }
            is StreamingClient.Event.Closed,
            is StreamingClient.Event.Failed -> Unit
        }
    }

    private fun finishCapture() {
        if (!captureGateActive) {
            // Never started — no streamer to finalize, just back out.
            finish()
            return
        }
        streamer?.finalize("user")
        // Give the server a beat to ack the finalize before we hop
        // to the detail screen. If the WS round-trip beats the
        // delay, handleStreamEvent will fire routeToCaptureDetail
        // first and the postDelayed will short-circuit on the
        // isFinishing() guard inside.
        binding.glSurface.postDelayed({
            if (!isFinishing) routeToCaptureDetail()
        }, 5_000)
    }

    /**
     * Hop the user to the native CaptureDetailActivity stacked on
     * top of MainActivity. Replaces the previous behaviour of
     * opening /captures/<id> in a browser.
     *
     * Intent flags:
     *   - CLEAR_TOP + SINGLE_TOP on the MainActivity intent so we
     *     reuse the existing instance instead of stacking a duplicate.
     *   - NEW_TASK is implicit because we're starting from an
     *     activity context with the MainActivity flags.
     */
    private fun routeToCaptureDetail() {
        val cap = captureId ?: return
        val home = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val detail = Intent(this, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, cap)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, captureName.orEmpty())
        }
        // Stack: MainActivity (root) <- CaptureDetailActivity (top).
        // Back from detail returns to home.
        startActivities(arrayOf(home, detail))
        finish()
    }

    private inner class Renderer : GLSurfaceView.Renderer {
        override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
            GLES20.glClearColor(0f, 0f, 0f, 1f)
            background.createOnGlThread()
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
            background.updateTexCoords(frame)
            background.draw()
            if (!captureGateActive) return
            val captured = ar.pollFrameData(frame) ?: return
            streamer?.sendFrame(
                idx = captured.idx,
                jpeg = captured.jpeg,
                pose = captured.pose,
                intrinsics = captured.intrinsics,
            )
        }
    }
}
