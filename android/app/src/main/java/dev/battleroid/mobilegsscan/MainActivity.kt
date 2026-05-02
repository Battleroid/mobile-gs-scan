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

/**
 * Home screen.
 *
 * Three responsibilities:
 *   1. Status indicator — polls /api/health every 5s, flips a green
 *      dot / "online" label or a red dot / "offline" label.
 *   2. Local drafts list — capture sessions recorded but not yet
 *      uploaded. Sourced from [DraftStore.listDrafts] and refreshed
 *      every poll tick alongside the captures list. Tapping a row
 *      opens [DraftDetailActivity] for upload / discard.
 *   3. Server captures list — polls /api/captures every 5s, renders
 *      rows via [CaptureAdapter]. Pull-to-refresh forces an
 *      immediate reload. Tapping a row opens
 *      [CaptureDetailActivity] for status / jobs / artifacts.
 *   4. New capture — creates a local [Draft] (no server call yet)
 *      and hands off to [CaptureActivity]. The capture activity
 *      records frames to the draft; what happens to the draft on
 *      Finish is the user's choice (upload now / save for later /
 *      discard). This means a fresh capture works fully offline —
 *      the studio doesn't need to be reachable on the recording
 *      device's current network.
 *
 * Falls back to a "configure your studio" empty state when
 * [ServerConfig.studioUrl] is unset, but only for the captures
 * list and health indicator — drafts work without a studio URL.
 *
 * Also handles the legacy https://<studio>/m/<token> deep link
 * intent for backwards compatibility with the web QR flow. Drafts
 * are filesDir-local so they survive process death and reboots.
 */
class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private val captureAdapter = CaptureAdapter(::onCaptureClicked)
    private val draftAdapter = DraftAdapter(::onDraftClicked)
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
        binding.captures.adapter = captureAdapter
        binding.drafts.layoutManager = LinearLayoutManager(this)
        binding.drafts.adapter = draftAdapter

        binding.btnSettings.setOnClickListener {
            startActivity(Intent(this, ServerConfigActivity::class.java))
        }

        binding.btnNewCapture.setOnClickListener { createNewCapture() }

        binding.refresh.setOnRefreshListener {
            lifecycleScope.launch {
                pollOnce()
                renderDrafts(DraftStore.listDrafts(this@MainActivity))
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
        renderDrafts(DraftStore.listDrafts(this))
        val studioUrl = ServerConfig.studioUrl(this)
        if (studioUrl == null) {
            renderNoUrl()
            client = null
            return
        }
        binding.statusUrl.text = studioUrl
        client = StudioClient(studioUrl)
        // New-capture is always enabled when we have either a
        // studio URL OR no studio URL — recording is local. We
        // only disable it when the user hasn't set a studio AND
        // hasn't set up ARCore yet (handled by the deep-link
        // path); for simplicity keep enabled here whenever the
        // url is set.
        binding.btnNewCapture.isEnabled = true
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
        // Captures list empty — but drafts can still exist.
        binding.captures.visibility = View.GONE
        binding.capturesHeader.visibility = View.GONE
        // New capture stays enabled even without a studio URL: the
        // user can record locally and configure / upload later.
        binding.btnNewCapture.isEnabled = true
        captureAdapter.submit(emptyList())
        // Show the no-url empty state only when there are no
        // drafts; otherwise the drafts list speaks for itself.
        if (draftAdapter.itemCount == 0) {
            binding.empty.visibility = View.VISIBLE
            binding.empty.text = getString(R.string.empty_no_url)
        } else {
            binding.empty.visibility = View.GONE
        }
    }

    private fun renderHealth(ok: Boolean) {
        lastHealthOk = ok
        binding.statusText.text = getString(
            if (ok) R.string.status_online else R.string.status_offline
        )
        binding.statusDot.setBackgroundResource(
            if (ok) R.drawable.dot_online else R.drawable.dot_offline
        )
    }

    private fun renderCaptures(list: List<StudioClient.Capture>) {
        captureAdapter.submit(list)
        val anyDrafts = draftAdapter.itemCount > 0
        if (list.isEmpty() && !anyDrafts) {
            binding.empty.visibility = View.VISIBLE
            binding.empty.text = getString(R.string.empty_no_captures)
            binding.captures.visibility = View.GONE
            binding.capturesHeader.visibility = View.GONE
        } else {
            binding.empty.visibility = View.GONE
            binding.captures.visibility = if (list.isEmpty()) View.GONE else View.VISIBLE
            binding.capturesHeader.visibility =
                if (list.isEmpty()) View.GONE else View.VISIBLE
        }
    }

    private fun renderDrafts(list: List<Draft>) {
        draftAdapter.submit(list)
        binding.drafts.visibility = if (list.isEmpty()) View.GONE else View.VISIBLE
        binding.draftsHeader.visibility = if (list.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun startPolling() {
        pollJob?.cancel()
        pollJob = lifecycleScope.launch {
            while (true) {
                pollOnce()
                renderDrafts(DraftStore.listDrafts(this@MainActivity))
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

    private fun onDraftClicked(d: Draft) {
        val baseUrl = ServerConfig.studioUrl(this).orEmpty()
        startActivity(
            Intent(this, DraftDetailActivity::class.java).apply {
                putExtra(DraftDetailActivity.EXTRA_BASE_URL, baseUrl)
                putExtra(DraftDetailActivity.EXTRA_DRAFT_ID, d.id)
                putExtra(DraftDetailActivity.EXTRA_AUTO_UPLOAD, false)
            },
        )
    }

    @Suppress("unused")
    private fun showError(msg: String) {
        AlertDialog.Builder(this)
            .setMessage(msg)
            .setPositiveButton("ok", null)
            .show()
    }

    private fun handleDeepLink(intent: Intent?) {
        // Legacy QR-token deep link path. Phone gets routed here via
        // a https://<studio>/m/<token> URL. Pre-pivot this would
        // open CaptureActivity directly with the pair token; now we
        // just absorb the studio URL and ignore the token (the
        // local-record flow doesn't need it). The token-based
        // server flow will be retired alongside this PR's web
        // /captures/new pairing UI in a follow-up.
        val data: Uri = intent?.data ?: return
        val segments = data.pathSegments
        if (segments.size < 2 || segments[0] != "m") return
        val baseUrl = "${data.scheme}://${data.host}${
            if (data.port > 0) ":${data.port}" else ""
        }"
        ServerConfig.setStudioUrl(this, baseUrl)
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

/**
 * RecyclerView adapter for the drafts list. Reuses
 * [ItemCaptureBinding] for visual parity — drafts and uploaded
 * captures share the "name + subtitle" row shape on the home
 * screen, just with different subtitle copy.
 */
class DraftAdapter(
    private val onClick: (Draft) -> Unit,
) : RecyclerView.Adapter<DraftAdapter.VH>() {

    private var items: List<Draft> = emptyList()

    fun submit(list: List<Draft>) {
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
        fun bind(d: Draft, onClick: (Draft) -> Unit) {
            val ctx = binding.root.context
            val m = d.meta
            binding.name.text = m.name ?: ctx.getString(R.string.draft_unnamed)
            val subtitleFmt = if (m.finalized) {
                R.string.draft_subtitle_finalized_fmt
            } else {
                R.string.draft_subtitle_incomplete_fmt
            }
            binding.subtitle.text = ctx.getString(subtitleFmt, m.frame_count, m.created_at)
            binding.root.setOnClickListener { onClick(d) }
        }
    }
}
