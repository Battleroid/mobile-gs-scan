package dev.battleroid.mobilegsscan

import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import dev.battleroid.mobilegsscan.ui.detail.JobDetailScreen
import dev.battleroid.mobilegsscan.ui.detail.JobDetailUiState
import dev.battleroid.mobilegsscan.ui.detail.LogPanelState
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Single-job detail screen — Compose port of the legacy XML-driven
 * JobDetailActivity. Polls `/api/jobs/{id}` every 3 s and renders
 * progress, message, error, claimed_by, timestamps, and the
 * worker's result blob (when present).
 *
 * Subprocess log panel:
 *  - Auto-opens the first time the job is observed running /
 *    claimed so the user gets the live tail without interaction.
 *  - Manual toggle via the show/hide pill afterwards.
 *  - When open, fetched on every 3 s job-poll tick + immediately
 *    when the panel is opened.
 *  - Auto-pins to the latest line on every content change (the
 *    Compose port runs this from a LaunchedEffect inside the log
 *    body composable; legacy used TextView.post + scrollTo).
 *
 * Result is pretty-printed JSON — worker output is freeform
 * (sfm metrics, train stats, export paths) and the rendering
 * audience is debugging a failed job, so the raw payload is the
 * useful thing to show.
 *
 * Retry: failed / canceled rows surface a tomato "Retry" CTA at
 * the bottom of the screen. Tapping POSTs ``/api/jobs/{id}/retry``
 * which enqueues a fresh job of the same kind + payload; the user
 * stays on this screen and the next poll tick can route them
 * forward if we ever want detail-page-on-success behavior. For
 * now a Toast confirms the new job was queued.
 */
class JobDetailActivity : ComponentActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_JOB_ID = "job_id"
        const val EXTRA_JOB_KIND = "job_kind"
    }

    private val state: MutableStateFlow<JobDetailUiState> =
        MutableStateFlow(
            JobDetailUiState(
                kind = "",
                job = null,
                networkError = null,
                log = LogPanelState.Initial,
            ),
        )
    private val uiState: StateFlow<JobDetailUiState> = state.asStateFlow()
    private var pollJob: Job? = null
    private var client: StudioClient? = null
    private var jobId: String = ""

    // Tracks whether we've already auto-opened the log this session.
    // The legacy implementation auto-opened once on the first
    // running/claimed observation; subsequent transitions back
    // through running do NOT re-open if the user has explicitly
    // closed the panel.
    private var lastStatus: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        jobId = intent.getStringExtra(EXTRA_JOB_ID).orEmpty()
        val seedKind = intent.getStringExtra(EXTRA_JOB_KIND).orEmpty()
        if (baseUrl.isEmpty() || jobId.isEmpty()) {
            finish()
            return
        }

        state.update { it.copy(kind = seedKind) }
        client = StudioClient(baseUrl)

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                JobDetailScreen(
                    state = current,
                    onBackClick = { finish() },
                    onToggleLog = ::toggleLog,
                    onRetryClick = ::onRetryClick,
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        pollJob?.cancel()
        pollJob = lifecycleScope.launch {
            while (true) {
                pollOnce()
                delay(3_000)
            }
        }
    }

    override fun onPause() {
        super.onPause()
        pollJob?.cancel()
        pollJob = null
    }

    private suspend fun pollOnce() {
        val c = client ?: return
        val detail = try {
            c.getJob(jobId)
        } catch (e: Exception) {
            state.update { it.copy(networkError = e.message ?: "unknown") }
            return
        }

        // Auto-open the log panel the first time we see the job
        // running / claimed. After that the user is in control.
        val running = detail.status == "running" || detail.status == "claimed"
        val wasRunning = lastStatus == "running" || lastStatus == "claimed"
        if (running && !wasRunning && !state.value.log.open) {
            state.update { it.copy(log = it.log.copy(open = true)) }
        }
        lastStatus = detail.status

        state.update {
            it.copy(
                kind = detail.kind,
                job = detail,
                networkError = null,
            )
        }

        if (state.value.log.open) {
            fetchAndRenderLog()
        }
    }

    private fun toggleLog() {
        val nowOpen = !state.value.log.open
        state.update { it.copy(log = it.log.copy(open = nowOpen)) }
        if (nowOpen) {
            // Fetch immediately so the user doesn't have to wait for
            // the next 3 s poll tick.
            lifecycleScope.launch { fetchAndRenderLog() }
        }
    }

    private fun onRetryClick() {
        val c = client ?: return
        if (state.value.retrying) return
        state.update { it.copy(retrying = true) }
        lifecycleScope.launch {
            try {
                c.retryJob(jobId)
                Toast.makeText(
                    this@JobDetailActivity,
                    "Queued a fresh job",
                    Toast.LENGTH_SHORT,
                ).show()
                // Force a poll tick so the row's status flips back
                // to ``queued`` / ``running`` visibly without waiting
                // for the next 3 s cycle. The polled row will keep
                // showing the ORIGINAL job (id didn't change) — but
                // the parent capture page will see the new job
                // appear in the pipeline list on its next refresh.
                pollOnce()
            } catch (e: Exception) {
                Toast.makeText(
                    this@JobDetailActivity,
                    "Retry failed: ${e.message ?: "unknown"}",
                    Toast.LENGTH_LONG,
                ).show()
            } finally {
                state.update { it.copy(retrying = false) }
            }
        }
    }

    private suspend fun fetchAndRenderLog() {
        val c = client ?: return
        val res = try {
            c.getJobLog(jobId)
        } catch (e: Exception) {
            state.update {
                it.copy(log = it.log.copy(fetchError = e.message ?: "unknown"))
            }
            return
        }
        state.update {
            it.copy(
                log = it.log.copy(
                    content = res.log,
                    available = res.available,
                    fetchError = null,
                ),
            )
        }
    }
}
