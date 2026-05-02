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
    }

    private lateinit var binding: ActivityJobDetailBinding
    private var pollJob: Job? = null
    private var client: StudioClient? = null
    private var jobId: String = ""

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
        renderJob(detail)
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
        binding.startedValue.text = j.started_at ?: “—”.toString()
        binding.completedValue.text = j.completed_at ?: “—”.toString()

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
