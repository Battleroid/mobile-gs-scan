package dev.battleroid.mobilegsscan

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.lifecycleScope
import androidx.webkit.WebViewAssetLoader
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Embedded splat-viewer screen. Hosts a chrome-less Chromium WebView
 * pointing at the bundled `assets/splat/index.html` page, which loads
 * `viewer.bundle.js` (Three.js + Spark + the entry from
 * `web/src/android-splat/entry.ts`) and renders the splat.
 *
 * Why an in-app WebView instead of native GLES: PR-D's research turned
 * up no off-the-shelf Android Gaussian-Splatting library that supports
 * `.spz` (the format our worker emits). The web app already ships a
 * Spark-based viewer; reusing it here keeps visuals identical to web
 * and the renderer maintained in one place. Compared to the previous
 * fallback (`ACTION_VIEW` against `/captures/{id}` in the user's
 * default browser), the bundled HTML loads instantly from disk, has
 * no nav chrome, and reads the splat from app private storage — which
 * means it works on a captive-portal LAN or offline once the asset is
 * cached.
 *
 * Asset routing: a [WebViewAssetLoader] serves two virtual prefixes
 * under `https://appassets.androidplatform.net/`:
 *   - `/assets/` (followed by any path) — the HTML + JS shipped in
 *     `assets/splat/`.
 *   - `/splats/` (followed by `<sceneId>.spz`) — the splat file
 *     downloaded for this scene.
 * Using a single https origin sidesteps Chromium's progressive
 * tightening of `file://` access (CORS + script eval rules) without
 * needing the deprecated `setAllowFileAccessFromFileURLs` knob.
 *
 * Asset lifecycle: each scene's `.spz` is cached under
 * `filesDir/splats/<scene_id>.spz`. The activity refuses to launch
 * without an absolute path/URL, so the only entry point — the capture
 * detail hero tap — must pass the scene id + relative artifact URL via
 * Intent extras. Re-tapping a recently-opened scene reuses the cached
 * file (.spz outputs from the worker are immutable per
 * `export.py`'s job-kind contract).
 */
class SplatViewerActivity : ComponentActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_SCENE_ID = "scene_id"
        // Relative URL from the Scene DTO (`/api/scenes/{id}/artifacts/spz`).
        // The activity resolves it against base_url before fetching.
        const val EXTRA_SPZ_URL = "spz_url"
        const val EXTRA_CAPTURE_NAME = "capture_name"

        /** WebViewAssetLoader's default authority. Same scheme + host
         *  used by every Google sample for AAR-served HTML; safe
         *  because Chromium grants the same-origin policy treatment
         *  as any https page. */
        private const val APP_ORIGIN = "https://appassets.androidplatform.net"
    }

    private val state: MutableStateFlow<SplatViewerUiState> =
        MutableStateFlow(SplatViewerUiState.Loading(progress = 0f))
    private val uiState: StateFlow<SplatViewerUiState> = state.asStateFlow()

    private lateinit var http: OkHttpClient
    private lateinit var captureName: String
    private lateinit var sceneId: String
    private lateinit var baseUrl: String
    private lateinit var spzUrl: String

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        // WebView is heavy and the page is interactive — keep the
        // screen on while the user is orbiting a splat, mirroring the
        // capture HUD's wake-lock posture.
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        sceneId = intent.getStringExtra(EXTRA_SCENE_ID).orEmpty()
        spzUrl = intent.getStringExtra(EXTRA_SPZ_URL).orEmpty()
        captureName = intent.getStringExtra(EXTRA_CAPTURE_NAME).orEmpty()
        if (baseUrl.isBlank() || sceneId.isBlank() || spzUrl.isBlank()) {
            finish()
            return
        }

        http = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            // .spz files are usually 5–60 MB; pad the read timeout so
            // a phone on a flaky LAN can still make forward progress.
            .readTimeout(120, TimeUnit.SECONDS)
            .build()

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                SplatViewerScreen(
                    state = current,
                    captureName = captureName,
                    onBackClick = { finish() },
                )
            }
        }

        lifecycleScope.launch { cacheSpzAndOpen() }
    }

    /** Resolve a relative artifact URL (e.g. `/api/scenes/{id}/artifacts/spz`)
     *  against the stored base. Absolute URLs pass through unchanged so
     *  this works if the server ever starts emitting absolute links. */
    private fun absoluteUrl(rel: String): String =
        if (rel.startsWith("http://") || rel.startsWith("https://"))
            rel
        else
            baseUrl.trimEnd('/') + "/" + rel.trimStart('/')

    /** Download the .spz once (or reuse the cached copy) and then
     *  swap UI state to [SplatViewerUiState.Ready] with the
     *  WebView-facing virtual URL the bundle should load. */
    private suspend fun cacheSpzAndOpen() {
        val cacheDir = File(filesDir, "splats").apply { mkdirs() }
        val cached = File(cacheDir, "$sceneId.spz")
        val url = absoluteUrl(spzUrl)

        if (!cached.exists() || cached.length() == 0L) {
            val ok = withContext(Dispatchers.IO) {
                val call = http.newCall(Request.Builder().url(url).build())
                // Hook OkHttp's cancellation to the surrounding
                // coroutine job. ``call.execute()`` is a blocking I/O
                // call that the IO dispatcher won't interrupt on
                // structured cancellation by itself, so back-press
                // would otherwise leave the download running until
                // the request body finished arriving (or the read
                // timeout elapsed). ``invokeOnCompletion`` fires
                // once the job terminates for any reason; calling
                // ``call.cancel()`` on an already-completed Call is
                // a no-op so the "happy path" overhead is zero.
                coroutineContext[Job]?.invokeOnCompletion { call.cancel() }
                try {
                    call.execute().use { res ->
                        if (!res.isSuccessful) {
                            error("HTTP ${res.code}")
                        }
                        val body = res.body ?: error("empty body")
                        val tmp = File(cacheDir, "$sceneId.spz.part")
                        body.byteStream().use { input ->
                            tmp.outputStream().use { output ->
                                val total = body.contentLength()
                                val buf = ByteArray(64 * 1024)
                                var read: Int
                                var done = 0L
                                while (input.read(buf).also { read = it } > 0) {
                                    output.write(buf, 0, read)
                                    done += read
                                    if (total > 0) {
                                        val p = (done.toDouble() / total)
                                            .coerceIn(0.0, 1.0)
                                        state.update {
                                            SplatViewerUiState.Loading(
                                                progress = p.toFloat(),
                                            )
                                        }
                                    }
                                }
                            }
                        }
                        // Atomic publish — Chromium watches the
                        // destination file and would otherwise race
                        // the download. ``renameTo`` returns false
                        // on filesystem errors (target locked,
                        // cross-volume on weird OEMs, permission
                        // flap); without the explicit check the
                        // activity would happily switch to Ready and
                        // have the WebView 404 on /splats/<id>.spz,
                        // leaving the user with a permanently
                        // broken cache entry (because next open also
                        // short-circuits on the ``exists() ||
                        // length() == 0L`` guard).
                        if (!tmp.renameTo(cached)) {
                            tmp.delete()
                            error("rename to ${cached.name} failed")
                        }
                    }
                    true
                } catch (ce: CancellationException) {
                    // Back-press or activity finish() cancelled the
                    // surrounding lifecycleScope; structured
                    // cancellation must propagate so the dispatcher
                    // unwinds the coroutine cleanly. The
                    // invokeOnCompletion hook above already aborted
                    // the OkHttp Call, but rethrow so any callers up
                    // the chain (none today, but defensively) also
                    // see the cancel rather than a fake "download
                    // failed" UI state.
                    throw ce
                } catch (e: Throwable) {
                    state.update {
                        SplatViewerUiState.Failed(
                            message = e.message ?: "download failed",
                        )
                    }
                    false
                }
            }
            if (!ok) return
        }

        // WebViewAssetLoader will serve this file under
        // `${APP_ORIGIN}/splats/<file-name>`; the bundle picks it up
        // through `?spz=<that url>`. The assets handler is registered
        // at `/assets/`, so the bundled HTML lives at
        // `/assets/splat/index.html` (assets-relative path retained
        // verbatim through the handler's lookup).
        val virtualSpz = "$APP_ORIGIN/splats/${cached.name}"
        state.update {
            SplatViewerUiState.Ready(
                pageUrl = "$APP_ORIGIN/assets/splat/index.html?spz=$virtualSpz",
                cachedSpzDir = cacheDir,
            )
        }
    }
}

sealed interface SplatViewerUiState {
    data class Loading(val progress: Float) : SplatViewerUiState
    data class Ready(val pageUrl: String, val cachedSpzDir: File) : SplatViewerUiState
    data class Failed(val message: String) : SplatViewerUiState
}

@Composable
private fun SplatViewerScreen(
    state: SplatViewerUiState,
    captureName: String,
    onBackClick: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Box(modifier = Modifier.fillMaxSize().background(pebble.bg)) {
        when (state) {
            is SplatViewerUiState.Ready -> SplatWebView(state = state)
            else -> Unit
        }

        // Top app strip — back chevron + caption. Floats on top of the
        // WebView so the renderer keeps the full viewport. Inset-aware
        // so it lands below the status bar on edge-to-edge.
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .windowInsetsPadding(WindowInsets.statusBars)
                .padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(36.dp)
                    .clip(CircleShape)
                    .background(Color.White.copy(alpha = 0.85f))
                    .clickable(onClick = onBackClick),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = "‹",
                    style = MaterialTheme.typography.titleLarge.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = pebble.ink,
                )
            }
            Spacer(Modifier.size(12.dp))
            Column {
                Text(
                    text = "SPLAT",
                    style = MaterialTheme.typography.labelSmall,
                    color = pebble.inkSoft,
                )
                if (captureName.isNotBlank()) {
                    Text(
                        text = captureName,
                        style = MaterialTheme.typography.titleMedium.copy(
                            fontWeight = FontWeight.SemiBold,
                        ),
                        color = pebble.ink,
                    )
                }
            }
        }

        // Loading / error overlay. Centered so it's the first thing
        // the eye reaches while the splat fetch is in flight.
        when (state) {
            is SplatViewerUiState.Loading -> LoadingOverlay(progress = state.progress)
            is SplatViewerUiState.Failed -> FailureOverlay(message = state.message)
            else -> Unit
        }
    }
}

@Composable
private fun LoadingOverlay(progress: Float) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = Modifier
            .fillMaxSize()
            .windowInsetsPadding(WindowInsets.systemBars),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier
                .clip(RoundedCornerShape(18.dp))
                .background(Color.White.copy(alpha = 0.9f))
                .padding(horizontal = 22.dp, vertical = 18.dp),
        ) {
            Text(
                text = "DOWNLOADING SPLAT",
                style = MaterialTheme.typography.labelSmall,
                color = pebble.inkSoft,
            )
            val bounded = progress.coerceIn(0f, 1f)
            // Indeterminate when the server didn't send Content-Length
            // (progress == 0 the whole way). Tracks pebble accent for
            // the determinate case.
            if (bounded > 0f) {
                LinearProgressIndicator(
                    progress = { bounded },
                    modifier = Modifier
                        .height(6.dp)
                        .fillMaxWidth(),
                    color = pebble.accent,
                    trackColor = pebble.rule,
                )
                Text(
                    text = "${(bounded * 100).toInt()}%",
                    style = MaterialTheme.typography.labelMedium,
                    color = pebble.ink,
                )
            } else {
                LinearProgressIndicator(
                    modifier = Modifier
                        .height(6.dp)
                        .fillMaxWidth(),
                    color = pebble.accent,
                    trackColor = pebble.rule,
                )
            }
        }
    }
}

@Composable
private fun FailureOverlay(message: String) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = Modifier
            .fillMaxSize()
            .windowInsetsPadding(WindowInsets.systemBars),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            modifier = Modifier
                .clip(RoundedCornerShape(14.dp))
                .background(pebble.danger.copy(alpha = 0.1f))
                .padding(horizontal = 18.dp, vertical = 14.dp),
        ) {
            Text(
                text = "COULDN'T LOAD SPLAT",
                style = MaterialTheme.typography.labelSmall,
                color = pebble.danger,
            )
            Spacer(Modifier.size(6.dp))
            Text(
                text = message,
                style = MaterialTheme.typography.bodyMedium,
                color = pebble.ink,
            )
        }
    }
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
private fun SplatWebView(state: SplatViewerUiState.Ready) {
    val pebble = MaterialTheme.pebble
    AndroidView(
        factory = { ctx ->
            val loader = WebViewAssetLoader.Builder()
                // Virtual `/assets/splat/<file>` maps to the app's
                // `assets/splat/<file>` dir. AssetsPathHandler strips the
                // registered prefix before resolving against the
                // APK's assets root, so a request for
                // `/assets/splat/viewer.bundle.js` opens
                // `assets/splat/viewer.bundle.js`. Registering at
                // `/assets/` (rather than `/`) keeps the handler
                // scoped — `/splats/<file>.spz` falls through to the
                // next handler below instead of being mistaken for an
                // APK asset.
                .addPathHandler(
                    "/assets/",
                    WebViewAssetLoader.AssetsPathHandler(ctx),
                )
                // /splats/<sceneId>.spz serves the cached .spz from
                // filesDir/splats/. InternalStoragePathHandler
                // refuses paths that escape the supplied directory,
                // so URL-fuzzing the WebView can't reach the rest of
                // private storage.
                .addPathHandler(
                    "/splats/",
                    WebViewAssetLoader.InternalStoragePathHandler(
                        ctx,
                        state.cachedSpzDir,
                    ),
                )
                .build()

            WebView(ctx).apply {
                setBackgroundColor(pebble.bg.toArgb())
                // Compose Android theme color → ARGB int (toArgb()
                // is an extension, brought in via the import below).
                settings.apply {
                    javaScriptEnabled = true
                    domStorageEnabled = true
                    // Required for the OffscreenCanvas + Worker bits
                    // Spark uses; setting it false here would silently
                    // strand the renderer on a blank canvas.
                    allowFileAccess = false
                    allowContentAccess = false
                    // The bundle is shipped as a regular asset and
                    // served via WebViewAssetLoader; we don't need the
                    // legacy file:// → file:// allowances.
                    @Suppress("DEPRECATION")
                    allowFileAccessFromFileURLs = false
                    @Suppress("DEPRECATION")
                    allowUniversalAccessFromFileURLs = false
                    mediaPlaybackRequiresUserGesture = false
                    cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
                }
                webViewClient = object : WebViewClient() {
                    override fun shouldInterceptRequest(
                        view: WebView,
                        request: WebResourceRequest,
                    ): WebResourceResponse? =
                        loader.shouldInterceptRequest(request.url)
                }
                visibility = View.VISIBLE
                loadUrl(state.pageUrl)
            }
        },
        modifier = Modifier.fillMaxSize(),
    )
}

