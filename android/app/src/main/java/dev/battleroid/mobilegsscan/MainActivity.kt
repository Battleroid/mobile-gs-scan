package dev.battleroid.mobilegsscan

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import dev.battleroid.mobilegsscan.ui.home.HomeScreen
import dev.battleroid.mobilegsscan.ui.home.HomeStatus
import dev.battleroid.mobilegsscan.ui.home.HomeUiState
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Home screen — Compose port of the prior XML-driven MainActivity.
 *
 * Same four responsibilities the legacy implementation had:
 *   1. **Status indicator** — polls `/api/health` every 5 s; the
 *      header pill flips between online (green), offline (red), and
 *      not-configured (dust) tones.
 *   2. **Local drafts list** — sourced from [DraftStore.listDrafts].
 *      Refreshed on every poll tick alongside the captures list and
 *      whenever the screen returns to foreground.
 *   3. **Server captures list** — polls `/api/captures` every 5 s;
 *      rows tap-through to [CaptureDetailActivity].
 *   4. **New capture** — spawns a local [Draft] and hands off to
 *      [CaptureActivity]. Works fully offline; the studio doesn't
 *      need to be reachable at recording time.
 *
 * Compose-vs-XML choices:
 *  - `ComponentActivity` instead of `AppCompatActivity` — the
 *    Compose surface doesn't need AppCompat's view-system bridge
 *    and dropping it saves a layer of inset / theme indirection.
 *  - State holder is a [MutableStateFlow] owned by the activity,
 *    not a ViewModel. Polling already restarts on every onResume
 *    so there's nothing to survive rotation; a ViewModel can be
 *    retrofitted later if that changes.
 *  - Window insets are driven via [enableEdgeToEdge] + per-element
 *    `WindowInsets.statusBars.asPaddingValues()` reads, replacing
 *    the XML's `setOnApplyWindowInsetsListener(...).updatePadding`.
 */
class MainActivity : ComponentActivity() {
    private val state: MutableStateFlow<HomeUiState> = MutableStateFlow(initialState())
    private val uiState: StateFlow<HomeUiState> = state.asStateFlow()
    private var pollJob: Job? = null
    private var client: StudioClient? = null

    private fun initialState(): HomeUiState = HomeUiState(
        status = HomeStatus.Resolving,
        studioHost = null,
        drafts = emptyList(),
        captures = emptyList(),
        canCreateNewCapture = true,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                HomeScreen(
                    state = current,
                    onSettingsClick = ::openSettings,
                    onNewCaptureClick = ::createNewCapture,
                    onCaptureClick = ::openCaptureDetail,
                    onDraftClick = ::openDraftDetail,
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        // Drafts can change while we were paused (a successful
        // upload deletes a draft from DraftStore, etc.). Refresh
        // once before the poll loop hits its first delay.
        refreshDrafts()

        val studioUrl = ServerConfig.studioUrl(this)
        if (studioUrl == null) {
            state.update {
                it.copy(
                    status = HomeStatus.NotConfigured,
                    studioHost = null,
                    captures = emptyList(),
                )
            }
            client = null
            return
        }
        state.update {
            it.copy(
                status = HomeStatus.Resolving,
                studioHost = studioHost(studioUrl),
            )
        }
        client = StudioClient(studioUrl)
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
                refreshDrafts()
                delay(5_000)
            }
        }
    }

    private suspend fun pollOnce() {
        val c = client ?: return
        val ok = try {
            c.health()
        } catch (_: Exception) {
            false
        }
        if (!ok) {
            state.update { it.copy(status = HomeStatus.Offline, captures = emptyList()) }
            return
        }
        val captures = try {
            c.listCaptures()
        } catch (_: Exception) {
            state.update { it.copy(status = HomeStatus.Offline) }
            return
        }
        state.update { it.copy(status = HomeStatus.Online, captures = captures) }
    }

    private fun refreshDrafts() {
        val drafts = DraftStore.listDrafts(this)
        state.update { it.copy(drafts = drafts) }
    }

    /**
     * Spawn a fresh local draft and hand off to CaptureActivity to
     * record into it. No server call here — the draft is purely
     * local until the user's "Upload now" choice. We capture the
     * current training-fidelity preset onto the draft so it
     * survives Settings changes between recording and upload.
     */
    private fun createNewCapture() {
        val baseUrl = ServerConfig.studioUrl(this).orEmpty()
        val iters = ServerConfig.captureTrainIters(this)
        val draft = DraftStore.newDraft(this, trainIters = iters)
        val intent = Intent(this, CaptureActivity::class.java).apply {
            putExtra(CaptureActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureActivity.EXTRA_DRAFT_ID, draft.id)
        }
        startActivity(intent)
    }

    private fun openSettings() {
        startActivity(Intent(this, ServerConfigActivity::class.java))
    }

    private fun openCaptureDetail(c: StudioClient.Capture) {
        val baseUrl = ServerConfig.studioUrl(this) ?: return
        startActivity(
            Intent(this, CaptureDetailActivity::class.java).apply {
                putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
                putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, c.id)
                putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, c.name)
            },
        )
    }

    private fun openDraftDetail(d: Draft) {
        val baseUrl = ServerConfig.studioUrl(this).orEmpty()
        startActivity(
            Intent(this, DraftDetailActivity::class.java).apply {
                putExtra(DraftDetailActivity.EXTRA_BASE_URL, baseUrl)
                putExtra(DraftDetailActivity.EXTRA_DRAFT_ID, d.id)
                putExtra(DraftDetailActivity.EXTRA_AUTO_UPLOAD, false)
            },
        )
    }

    /**
     * Pretty-print the studio URL's authority section for the
     * header pill. ServerConfig stores `https://host[:port]/...`
     * after normalisation; the pill is too narrow for the full
     * URL, so we strip the scheme and trailing slash.
     */
    private fun studioHost(url: String): String =
        url.removePrefix("https://").removePrefix("http://").removeSuffix("/")
}
