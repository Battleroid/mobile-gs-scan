package dev.battleroid.mobilegsscan

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.widget.Toast
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
 *   1. Validate ARCore is installed (deferred install if not).
 *   2. Request camera permission.
 *   3. Create the ARCore session + the streaming client.
 *   4. Render the camera background on the GLSurfaceView; pull
 *      pose+image frames in onDrawFrame and ship them to the server.
 *   5. On finish (button or back), call streamer.finalize() and pop
 *      the activity.
 *
 * The render path here is intentionally minimal — no overlay
 * geometry, just the camera background. PR #2 lands the proper
 * coverage-cone visualization.
 */
class CaptureActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_PAIR_TOKEN = "pair_token"
        const val EXTRA_CAPTURE_ID = "capture_id"
        private const val PERM_REQ = 0xC4
    }

    private lateinit var binding: ActivityCaptureBinding
    private var arSession: ARCaptureSession? = null
    private var streamer: StreamingClient? = null
    private var heartbeatJob: Job? = null

    private var baseUrl: String = ""
    private var pairToken: String = ""
    private var captureId: String? = null
    private var sceneId: String? = null

    private var receivedCount = 0
    private var droppedCount = 0

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
        binding.glSurface.setEGLContextClientVersion(2)
        binding.glSurface.setRenderer(Renderer())
        binding.glSurface.renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY

        ensurePermissionsThenConnect()
    }

    override fun onResume() {
        super.onResume()
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
        if (!avail.isSupported) {
            Toast.makeText(this, "ARCore not supported on this device", Toast.LENGTH_LONG).show()
            finish()
            return
        }
        try {
            arSession = ARCaptureSession(this)
        } catch (e: Exception) {
            Toast.makeText(this, "ARCore session failed: ${e.message}", Toast.LENGTH_LONG).show()
            finish()
            return
        }

        lifecycleScope.launch {
            val info = withContext(Dispatchers.IO) { resolveCaptureFromToken() }
            if (info == null) {
                Toast.makeText(this@CaptureActivity, "pair token invalid", Toast.LENGTH_LONG).show()
                finish()
                return@launch
            }
            captureId = info.captureId
            binding.sessionLabel.text = info.captureName

            streamer = StreamingClient(
                scope = lifecycleScope,
                baseUrl = baseUrl,
                captureId = info.captureId,
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
        }
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
                openProgressInBrowser()
            }
            is StreamingClient.Event.Closed,
            is StreamingClient.Event.Failed -> Unit
        }
    }

    private fun finishCapture() {
        streamer?.finalize("user")
        binding.glSurface.postDelayed({
            if (sceneId == null) openProgressInBrowser()
        }, 5_000)
    }

    private fun openProgressInBrowser() {
        val cap = captureId ?: return
        val url = "$baseUrl/captures/$cap"
        startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url)))
        finish()
    }

    private inner class Renderer : GLSurfaceView.Renderer {
        private var textureId: Int = 0

        override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
            GLES20.glClearColor(0f, 0f, 0f, 1f)
            val handles = IntArray(1)
            GLES20.glGenTextures(1, handles, 0)
            textureId = handles[0]
            GLES20.glBindTexture(0x8D65 /* GL_TEXTURE_EXTERNAL_OES */, textureId)
            GLES20.glTexParameteri(0x8D65, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
            GLES20.glTexParameteri(0x8D65, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
            arSession?.setTextureName(textureId)
        }

        override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
            GLES20.glViewport(0, 0, width, height)
            arSession?.setDisplayGeometry(windowManager.defaultDisplay.rotation, width, height)
        }

        override fun onDrawFrame(gl: GL10?) {
            GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
            val ar = arSession ?: return
            val captured = ar.pollFrame() ?: return
            // Drawing the camera background quad + overlay geometry
            // lands in PR #2 alongside the coverage cones.
            streamer?.sendFrame(
                idx = captured.idx,
                jpeg = captured.jpeg,
                pose = captured.pose,
                intrinsics = captured.intrinsics,
            )
        }
    }
}
