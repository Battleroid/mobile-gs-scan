package dev.battleroid.mobilegsscan

import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import dev.battleroid.mobilegsscan.databinding.ActivityMainBinding
import dev.battleroid.mobilegsscan.databinding.ItemCaptureBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * Home screen.
 *
 * Three responsibilities:
 *   1. Status indicator — polls /api/health every 5s, flips a green
 *      dot / "online" label or a red dot / "offline" label.
 *   2. Sessions list — polls /api/captures every 5s, renders rows
 *      via [CaptureAdapter]. Pull-to-refresh forces an immediate
 *      reload. Tapping a row opens [CaptureDetailActivity] for a
 *      native session view (status, jobs, artifacts).
 *   3. New capture — POSTs to /api/captures, then hands off to
 *      [CaptureActivity] with the freshly-issued capture id +
 *      pair_token. No QR pairing.
 *
 * Falls back to a "configure your studio" empty state when
 * [ServerConfig.studioUrl] is unset.
 *
 * Also handles the legacy https://<studio>/m/<token> deep link
 * intent for backwards compatibility with the web QR flow.
 */
class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private val adapter = CaptureAdapter(::onCaptureClicked)
    private var pollJob: Job? = null
    private var client: StudioClient? = null
    private var lastHealthOk: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

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

        binding.captures.layoutManager = LinearLayoutManager(this)
        binding.captures.adapter = adapter

        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, ServerConfigActivity::class.java))
        }

        binding.btnNewCapture.setOnClickListener { createNewCapture() }

        binding.refresh.setOnRefreshListener {
            lifecycleScope.launch {
                pollOnce()
                binding.refresh.isRefreshing = false
            }
        }

        handleDeepLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    override fun onResume() {
        super.onResume()
        val studioUrl = ServerConfig.studioUrl(this)
        if (studioUrl == null) {
            renderNoUrl()
            client = null
            return
        }
        binding.statusUrl.text = studioUrl
        client = StudioClient(studioUrl)
        startPolling()
    }

    override fun onPause() {
        super.onPause()
        pollJob?.cancel()
        pollJob = null
    }

    private fun renderNoUrl() {
        binding.statusText.text = getString(R.string.status_no_url)
        binding.statusUrl.text = ""
        binding.statusDot.setBackgroundResource(R.drawable.dot_offline)
        binding.captures.visibility = View.GONE
        binding.empty.visibility = View.VISIBLE
        binding.empty.text = getString(R.string.empty_no_url)
        binding.btnNewCapture.isEnabled = false
        adapter.submit(emptyList())
    }

    private fun renderHealth(ok: Boolean) {
        lastHealthOk = ok
        binding.statusText.text = getString(
            if (ok) R.string.status_online else R.string.status_offline
        )
        binding.statusDot.setBackgroundResource(
            if (ok) R.drawable.dot_online else R.drawable.dot_offline
        )
        binding.btnNewCapture.isEnabled = ok
    }

    private fun renderCaptures(list: List<StudioClient.Capture>) {
        adapter.submit(list)
        binding.empty.visibility = if (list.isEmpty()) View.VISIBLE else View.GONE
        binding.captures.visibility = if (list.isEmpty()) View.GONE else View.VISIBLE
        binding.empty.text = getString(R.string.empty_no_captures)
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
        val ok = c.health()
        renderHealth(ok)
        if (!ok) return
        try {
            renderCaptures(c.listCaptures())
        } catch (e: Exception) {
            binding.statusText.text = "${getString(R.string.status_offline)}: ${e.message}"
            binding.statusDot.setBackgroundResource(R.drawable.dot_offline)
        }
    }

    private fun createNewCapture() {
        val c = client ?: return
        val iters = ServerConfig.captureTrainIters(this)
        binding.btnNewCapture.isEnabled = false
        lifecycleScope.launch {
            try {
                // Send the user's chosen training-fidelity preset
                // through capture.meta so the worker's dispatch step
                // can override its env-default GS_TRAIN_ITERS for
                // this capture only.
                //
                // name=null lets the server pick a memorable random
                // name (`<adjective> <color> <noun>`). Users can
                // rename via the capture-detail screen if they want
                // something specific.
                val meta = buildJsonObject {
                    put("train_iters", JsonPrimitive(iters))
                }
                val capture = c.createCapture(
                    name = null,
                    hasPose = true,
                    meta = meta,
                )
                val token = capture.pair_token
                    ?: error("server returned no pair_token")
                val baseUrl = ServerConfig.studioUrl(this@MainActivity)
                    ?: error("studio URL cleared mid-flight")
                val intent = Intent(this@MainActivity, CaptureActivity::class.java).apply {
                    putExtra(CaptureActivity.EXTRA_BASE_URL, baseUrl)
                    putExtra(CaptureActivity.EXTRA_CAPTURE_ID, capture.id)
                    putExtra(CaptureActivity.EXTRA_CAPTURE_NAME, capture.name)
                    putExtra(CaptureActivity.EXTRA_PAIR_TOKEN, token)
                }
                startActivity(intent)
            } catch (e: Exception) {
                showError("could not start a new capture: ${e.message}")
            } finally {
                binding.btnNewCapture.isEnabled = lastHealthOk
            }
        }
    }

    private fun onCaptureClicked(c: StudioClient.Capture) {
        val baseUrl = ServerConfig.studioUrl(this) ?: return
        startActivity(
            Intent(this, CaptureDetailActivity::class.java).apply {
                putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
                putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, c.id)
                putExtra(CaptureDetailActivity.EXTRA_CAPTURE_NAME, c.name)
            },
        )
    }

    private fun showError(msg: String) {
        AlertDialog.Builder(this)
            .setMessage(msg)
            .setPositiveButton("ok", null)
            .show()
    }

    private fun handleDeepLink(intent: Intent?) {
        val data: Uri = intent?.data ?: return
        val segments = data.pathSegments
        if (segments.size < 2 || segments[0] != "m") return
        val token = segments[1].takeIf { it.isNotBlank() } ?: return
        val baseUrl = "${data.scheme}://${data.host}${
            if (data.port > 0) ":${data.port}" else ""
        }"
        ServerConfig.setStudioUrl(this, baseUrl)
        startActivity(
            Intent(this, CaptureActivity::class.java).apply {
                putExtra(CaptureActivity.EXTRA_BASE_URL, baseUrl)
                putExtra(CaptureActivity.EXTRA_PAIR_TOKEN, token)
            },
        )
    }
}

/** RecyclerView adapter for the captures list. */
class CaptureAdapter(
    private val onClick: (StudioClient.Capture) -> Unit,
) : RecyclerView.Adapter<CaptureAdapter.VH>() {

    private var items: List<StudioClient.Capture> = emptyList()

    fun submit(list: List<StudioClient.Capture>) {
        items = list
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
        val binding = ItemCaptureBinding.inflate(
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

    class VH(private val binding: ItemCaptureBinding) :
        RecyclerView.ViewHolder(binding.root) {
        fun bind(c: StudioClient.Capture, onClick: (StudioClient.Capture) -> Unit) {
            binding.name.text = c.name
            binding.subtitle.text = buildString {
                append(c.source)
                append(" · ${c.frame_count} frames")
                if (c.dropped_count > 0) append(" (${c.dropped_count} dropped)")
                append(" · ${c.status}")
            }
            binding.root.setOnClickListener { onClick(c) }
        }
    }
}
