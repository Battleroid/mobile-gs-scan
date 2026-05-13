package dev.battleroid.mobilegsscan

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import dev.battleroid.mobilegsscan.ui.detail.CaptureDetailScreen
import dev.battleroid.mobilegsscan.ui.detail.CaptureDetailUiState
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Native session-detail screen — Compose port of the legacy
 * XML-driven CaptureDetailActivity. Polls `/api/captures/{id}`
 * every 5 s for live status + frame counts. Once a scene exists,
 * also polls `/api/scenes/{scene_id}` for the pipeline jobs and
 * artifact URLs.
 *
 * Tapping a job row routes to [JobDetailActivity]. Tapping the
 * capture title opens an inline rename dialog (optimistic local
 * update; the next poll re-renders from the server row).
 * "Open viewer in browser" routes to the studio web UI for the
 * splat viewer — native splat rendering is out of scope here.
 */
class CaptureDetailActivity : ComponentActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_CAPTURE_ID = "capture_id"
        const val EXTRA_CAPTURE_NAME = "capture_name"
    }

    private val state: MutableStateFlow<CaptureDetailUiState> =
        MutableStateFlow(CaptureDetailUiState.Initial)
    private val uiState: StateFlow<CaptureDetailUiState> = state.asStateFlow()
    private var pollJob: Job? = null
    private var client: StudioClient? = null
    private var baseUrl: String = ""
    private var captureId: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        captureId = intent.getStringExtra(EXTRA_CAPTURE_ID).orEmpty()
        val seedName = intent.getStringExtra(EXTRA_CAPTURE_NAME).orEmpty()
        if (baseUrl.isEmpty() || captureId.isEmpty()) {
            finish()
            return
        }

        state.update { it.copy(captureName = seedName, baseUrl = baseUrl) }
        client = StudioClient(baseUrl)

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                CaptureDetailScreen(
                    state = current,
                    onBackClick = { finish() },
                    onRenameSubmit = ::submitRename,
                    onJobClick = ::onJobClicked,
                    onOpenViewerClick = ::openSceneInBrowser,
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        startPolling()
    }

    override fun onPause() {
        super.onPause()
        pollJob?.cancel()
        pollJob = null
    }

    private fun startPolling() {
        pollJob?.cancel()
        pollJob = lifecycleScope.launch {
            while (true) {
                pollOnce()
                delay(5_000)
            }
        }
    }

    private suspend fun pollOnce() {
        val c = client ?: return
        val capture = try {
            c.getCapture(captureId)
        } catch (e: Exception) {
            state.update { it.copy(networkError = e.message ?: "unknown") }
            return
        }
        state.update {
            it.copy(
                captureName = capture.name,
                capture = capture,
                networkError = null,
            )
        }
        val sceneId = capture.scene_id
        if (sceneId.isNullOrBlank()) {
            state.update { it.copy(sceneMissing = true, scene = null) }
            return
        }
        val scene = try {
            c.getScene(sceneId)
        } catch (_: Exception) {
            // Capture knows about the scene but scene fetch failed —
            // keep the prior scene snapshot and continue polling.
            // Common briefly during the create-scene → commit window.
            return
        }
        state.update { it.copy(sceneMissing = false, scene = scene) }
    }

    private fun submitRename(newName: String) {
        val c = client ?: return
        val previous = state.value.captureName
        // Optimistic local update; next poll will overwrite either
        // way (same value on success, prior value on failure path
        // through Toast below).
        state.update { it.copy(captureName = newName, renaming = true) }
        lifecycleScope.launch {
            try {
                c.renameCapture(captureId, newName)
                state.update { it.copy(renaming = false) }
            } catch (e: Exception) {
                state.update { it.copy(captureName = previous, renaming = false) }
                Toast.makeText(
                    this@CaptureDetailActivity,
                    "rename failed: ${e.message ?: "unknown"}",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    private fun onJobClicked(job: StudioClient.JobView) {
        startActivity(
            Intent(this, JobDetailActivity::class.java).apply {
                putExtra(JobDetailActivity.EXTRA_BASE_URL, baseUrl)
                putExtra(JobDetailActivity.EXTRA_JOB_ID, job.id)
                putExtra(JobDetailActivity.EXTRA_JOB_KIND, job.kind)
            },
        )
    }

    private fun openSceneInBrowser() {
        // The web side has no /scenes/{id} route; the splat viewer
        // lives on /captures/{captureId}. The legacy URL was a
        // leftover from an earlier route shape and 404'd every time.
        val url = "$baseUrl/captures/$captureId"
        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
    }
}
