package dev.battleroid.mobilegsscan

import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import dev.battleroid.mobilegsscan.databinding.ActivityCaptureDetailBinding
import dev.battleroid.mobilegsscan.databinding.ItemJobBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Native session-detail screen. Replaces the AlertDialog from the
 * pre-PR home screen and the "open in browser" hand-off the capture
 * activity used after Finish.
 *
 * Polls /api/captures/{id} every few seconds for live status +
 * frame counts. Once a scene exists (server-side: triggered when
 * the phone finalizes), also polls /api/scenes/{scene_id} for the
 * pipeline jobs and artifact URLs.
 *
 * Tapping a job row routes to [JobDetailActivity] for the deeper
 * view (progress bar, message, error, result blob). Tapping
 * "Open viewer in browser" deep-links to the studio web UI for
 * the splat viewer — native splat rendering is a bigger problem
 * than this PR is solving.
 *
 * Tapping the capture-name TextView opens a rename dialog (mirrors
 * the inline rename on the web detail page). The local TextView
 * updates immediately; the next poll re-renders from the server
 * row to confirm.
 */
class CaptureDetailActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_CAPTURE_ID = "capture_id"
        const val EXTRA_CAPTURE_NAME = "capture_name"
    }

    private lateinit var binding: ActivityCaptureDetailBinding
    private val adapter = JobAdapter(::onJobClicked)
    private var pollJob: Job? = null
    private var client: StudioClient? = null
    private var baseUrl: String = ""
    private var captureId: String = ""
    private var lastSceneId: String? = null
    private var lastPlyUrl: String? = null
    private var lastSpzUrl: String? = null
    private var currentName: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCaptureDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)

        baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        captureId = intent.getStringExtra(EXTRA_CAPTURE_ID).orEmpty()
        val seedName = intent.getStringExtra(EXTRA_CAPTURE_NAME).orEmpty()
        if (baseUrl.isEmpty() || captureId.isEmpty()) {
            finish()
            return
        }

        // Same edge-to-edge handling MainActivity / Settings get.
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

        currentName = seedName
        binding.captureName.text =
            seedName.ifBlank { getString(R.string.detail_loading) }
        binding.captureName.setOnClickListener { showRenameDialog() }
        binding.btnBack.setOnClickListener { finish() }
        binding.btnViewerWeb.setOnClickListener { openSceneInBrowser() }

        binding.jobs.layoutManager = LinearLayoutManager(this)
        binding.jobs.adapter = adapter

        client = StudioClient(baseUrl)
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
            binding.statusValue.text = getString(R.string.status_offline)
            binding.statusValue.append(": ${e.message ?: "unknown"}")
            return
        }
        renderCapture(capture)
        val sceneId = capture.scene_id
        if (sceneId.isNullOrBlank()) {
            renderNoScene()
            return
        }
        lastSceneId = sceneId
        val scene = try {
            c.getScene(sceneId)
        } catch (e: Exception) {
            // Capture knows about the scene but the scene fetch
            // failed — show what we have and keep polling. Common
            // briefly during the create-scene -> commit window.
            return
        }
        renderScene(scene)
    }

    private fun renderCapture(c: StudioClient.Capture) {
        currentName = c.name
        binding.captureName.text = c.name
        binding.statusValue.text = c.status
        binding.sourceValue.text = c.source
        binding.framesValue.text = if (c.dropped_count > 0) {
            getString(R.string.detail_frames_with_drops_fmt, c.frame_count, c.dropped_count)
        } else {
            getString(R.string.detail_frames_fmt, c.frame_count)
        }
        binding.createdValue.text = c.created_at
        if (c.error.isNullOrBlank()) {
            binding.errorRow.visibility = View.GONE
        } else {
            binding.errorRow.visibility = View.VISIBLE
            binding.errorValue.text = c.error
        }
    }

    private fun renderNoScene() {
        binding.scenePlaceholder.visibility = View.VISIBLE
        binding.jobsHeader.visibility = View.GONE
        binding.jobs.visibility = View.GONE
        binding.btnViewerWeb.visibility = View.GONE
        adapter.submit(emptyList())
    }

    private fun renderScene(scene: StudioClient.Scene) {
        binding.scenePlaceholder.visibility = View.GONE
        binding.jobsHeader.visibility = View.VISIBLE
        binding.jobs.visibility = View.VISIBLE
        adapter.submit(scene.jobs)
        lastPlyUrl = scene.ply_url
        lastSpzUrl = scene.spz_url
        // Show the "open viewer" CTA only once the export has
        // actually produced an artifact. Until then there's nothing
        // for the web viewer to render and the button would 404.
        binding.btnViewerWeb.visibility =
            if (!scene.ply_url.isNullOrBlank() || !scene.spz_url.isNullOrBlank()) {
                View.VISIBLE
            } else {
                View.GONE
            }
    }

    private fun showRenameDialog() {
        val padding = (24 * resources.displayMetrics.density).toInt()
        val input = EditText(this).apply {
            hint = getString(R.string.rename_capture_hint)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_WORDS
            setText(currentName)
            setSelection(text.length)
        }
        val container = android.widget.FrameLayout(this).apply {
            setPadding(padding, padding / 2, padding, 0)
            addView(input)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.rename_capture_title)
            .setView(container)
            .setPositiveButton(R.string.action_save) { _, _ ->
                val newName = input.text.toString().trim()
                if (newName.isEmpty() || newName == currentName) return@setPositiveButton
                submitRename(newName)
            }
            .setNegativeButton(R.string.action_cancel, null)
            .show()
    }

    private fun submitRename(newName: String) {
        val c = client ?: return
        // Optimistic local update so the title flips immediately;
        // the next poll will overwrite either way (with the same
        // value on success, or the old one on failure).
        val previous = currentName
        currentName = newName
        binding.captureName.text = newName
        lifecycleScope.launch {
            try {
                c.renameCapture(captureId, newName)
            } catch (e: Exception) {
                currentName = previous
                binding.captureName.text = previous
                Toast.makeText(
                    this@CaptureDetailActivity,
                    getString(R.string.rename_failed_fmt, e.message ?: "unknown"),
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    private fun onJobClicked(job: StudioClient.JobView) {
        val intent = Intent(this, JobDetailActivity::class.java).apply {
            putExtra(JobDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(JobDetailActivity.EXTRA_JOB_ID, job.id)
            putExtra(JobDetailActivity.EXTRA_JOB_KIND, job.kind)
        }
        startActivity(intent)
    }

    private fun openSceneInBrowser() {
        val sceneId = lastSceneId ?: return
        val url = "$baseUrl/scenes/$sceneId"
        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
    }
}

/** RecyclerView adapter for the jobs list. */
class JobAdapter(
    private val onClick: (StudioClient.JobView) -> Unit,
) : RecyclerView.Adapter<JobAdapter.VH>() {

    private var items: List<StudioClient.JobView> = emptyList()

    fun submit(list: List<StudioClient.JobView>) {
        items = list
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
        val binding = ItemJobBinding.inflate(
            LayoutInflater.from(parent.context),
            parent,
            false,
        )
        return VH(binding)
    }

    override fun onBindViewHolder(holder: VH, position: Int) {
        holder.bind(items[position], onClick)
    }

    override fun getItemCount(): Int = items.size

    class VH(private val binding: ItemJobBinding) :
        RecyclerView.ViewHolder(binding.root) {
        fun bind(j: StudioClient.JobView, onClick: (StudioClient.JobView) -> Unit) {
            binding.kind.text = j.kind
            binding.status.text = j.status
            binding.progressBar.progress = (j.progress * 100f).toInt()
            binding.progressMsg.text = j.progress_msg ?: ""
            binding.progressMsg.visibility =
                if (j.progress_msg.isNullOrBlank()) View.GONE else View.VISIBLE
            binding.root.setOnClickListener { onClick(j) }
        }
    }
}
