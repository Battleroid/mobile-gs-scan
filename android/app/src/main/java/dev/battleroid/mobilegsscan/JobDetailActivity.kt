package dev.battleroid.mobilegsscan

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import dev.battleroid.mobilegsscan.databinding.ActivityJobDetailBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement

/**
 * Native single-job detail screen. Polls /api/jobs/{id} every few
 * seconds and renders progress, message, error, and the worker's
 * result blob (when present).
 *
 * Subprocess log section: collapsible block at the bottom of the
 * card. Auto-opens for jobs that are currently running so the user
 * gets a live tail without interaction; closes itself when the job
 * lands but stays openable for postmortem inspection. Mirrors the
 * web's JobLogPanel behavior exactly. Polled at the same 3s tick as
 * the job-detail fetch when open + running.
 *
 * The result blob is intentionally rendered as pretty-printed JSON
 * — it's worker-specific (sfm output, train metrics, export paths)
 * and baking each shape into the client UI is more work than it's
 * worth right now. Folks reading the screen are usually trying to
 * debug a failed job, so the raw payload is what they want to see
 * anyway.
 */
class JobDetailActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_JOB_ID = "job_id"
        const val EXTRA_JOB_KIND = "job_kind"
        // En-dash placeholder for not-yet-set timestamps. Pulled out
        // as a const so the literal stays under our control — inline
        // "—" elsewhere in this file would risk getting smart-quoted
        // again by an editor / paste roundtrip.
        private const val UNSET = "—"
    }

    private lateinit var binding: ActivityJobDetailBinding
    private var pollJob: Job? = null
    private var client: StudioClient? = null
    private var jobId: String = ""

    // Log panel state. Default-open for running / claimed jobs so
    // the live tail shows immediately; default-closed otherwise.
    // Whether to actually fetch/show is then gated on this flag.
    private var logOpen: Boolean = false
    // Latest known job status, used by both the renderer (which job
    // detail data populates) and the log poller (which decides
    // whether to keep ticking). Kept here so the log fetch path
    // doesn't have to refetch the job just to know if it's running.
    private var lastStatus: String = ""

    private val prettyJson = Json { prettyPrint = true; encodeDefaults = false }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityJobDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        jobId = intent.getStringExtra(EXTRA_JOB_ID).orEmpty()
        val seedKind = intent.getStringExtra(EXTRA_JOB_KIND).orEmpty()
        if (baseUrl.isEmpty() || jobId.isEmpty()) {
            finish()
            return
        }

        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { v, insets ->
            val sys = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.updatePadding(
                left = sys.left,
                top = sys.top,
                right = sys.right,
                bottom = sys.bottom,
            )
            insets
        }

        binding.btnBack.setOnClickListener { finish() }
        binding.btnLogToggle.setOnClickListener { toggleLog() }
        binding.kind.text = seedKind.ifBlank { getString(R.string.detail_loading) }
        binding.statusValue.text = getString(R.string.detail_loading)

        client = StudioClient(baseUrl)
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
            binding.statusValue.text = getString(R.string.status_offline)
            binding.statusValue.append(": ${e.message ?: "unknown"}")
            return
        }

        // First time we see a running job, auto-open the log so the
        // user gets the live tail. After that the user is in
        // control via the toggle button.
        val running = detail.status == "running" || detail.status == "claimed"
        if (running && lastStatus != "running" && lastStatus != "claimed" && !logOpen) {
            logOpen = true
            applyLogVisibility()
        }
        lastStatus = detail.status

        renderJob(detail)
        if (logOpen) {
            fetchAndRenderLog()
        }
    }

    private fun toggleLog() {
        logOpen = !logOpen
        applyLogVisibility()
        if (logOpen) {
            // Fetch immediately when opened so the user doesn't have
            // to wait for the next 3s tick.
            lifecycleScope.launch { fetchAndRenderLog() }
        }
    }

    private fun applyLogVisibility() {
        binding.btnLogToggle.text = getString(
            if (logOpen) R.string.detail_log_hide else R.string.detail_log_show,
        )
        binding.logValue.visibility = if (logOpen) View.VISIBLE else View.GONE
    }

    private suspend fun fetchAndRenderLog() {
        val c = client ?: return
        val res = try {
            c.getJobLog(jobId)
        } catch (e: Exception) {
            binding.logValue.text = getString(
                R.string.detail_log_fetch_failed_fmt, e.message ?: "unknown",
            )
            return
        }
        if (!res.available) {
            binding.logValue.text = getString(R.string.detail_log_unavailable)
            return
        }
        binding.logValue.text = res.log.ifBlank { getString(R.string.detail_log_empty) }
    }

    private fun renderJob(j: StudioClient.JobDetail) {
        binding.kind.text = j.kind
        binding.statusValue.text = j.status
        binding.progressBar.progress = (j.progress * 100f).toInt()
        binding.progressPercent.text = getString(
            R.string.detail_progress_pct_fmt,
            (j.progress * 100f).toInt(),
        )
        binding.progressMsg.text = j.progress_msg ?: ""
        binding.progressMsg.visibility =
            if (j.progress_msg.isNullOrBlank()) View.GONE else View.VISIBLE

        binding.claimedByValue.text = j.claimed_by ?: getString(R.string.detail_unclaimed)
        binding.startedValue.text = j.started_at ?: UNSET
        binding.completedValue.text = j.completed_at ?: UNSET

        if (j.error.isNullOrBlank()) {
            binding.errorRow.visibility = View.GONE
        } else {
            binding.errorRow.visibility = View.VISIBLE
            binding.errorValue.text = j.error
        }

        val result: JsonElement? = j.result
        if (result == null) {
            binding.resultRow.visibility = View.GONE
        } else {
            binding.resultRow.visibility = View.VISIBLE
            binding.resultValue.text = prettyJson.encodeToString(
                JsonElement.serializer(),
                result,
            )
        }
    }
}
