package dev.battleroid.mobilegsscan

import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import dev.battleroid.mobilegsscan.databinding.ActivityDraftDetailBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/**
 * Local-draft detail screen.
 *
 * Lists the draft's frame count + created/updated timestamps,
 * lets the user rename it (the local name carries over to the
 * server name on upload), and offers Upload Now / Discard
 * actions. While an upload is in flight, the action row collapses
 * into a progress bar + Cancel button.
 *
 * Auto-upload mode: when launched with EXTRA_AUTO_UPLOAD=true the
 * activity kicks off the upload immediately (this is the path
 * CaptureActivity uses on the user's "Upload now" choice). When
 * launched without that flag (the path the home-screen drafts
 * list uses), the user has to tap Upload Now explicitly.
 *
 * On successful upload (server has acked the queued event) the
 * local draft directory is deleted and we route forward to the
 * server-side [CaptureDetailActivity] so the user lands on the
 * pipeline-progress view they're used to. On failure we leave the
 * draft in place and surface the error.
 */
class DraftDetailActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_DRAFT_ID = "draft_id"
        const val EXTRA_AUTO_UPLOAD = "auto_upload"
    }

    private lateinit var binding: ActivityDraftDetailBinding
    private var draft: Draft? = null
    private var baseUrl: String = ""
    private var uploadJob: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityDraftDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)

        baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        val draftId = intent.getStringExtra(EXTRA_DRAFT_ID).orEmpty()
        val autoUpload = intent.getBooleanExtra(EXTRA_AUTO_UPLOAD, false)
        if (draftId.isEmpty()) {
            finish()
            return
        }
        draft = DraftStore.openDraft(this, draftId)
        if (draft == null) {
            Toast.makeText(this, "draft no longer exists", Toast.LENGTH_SHORT).show()
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
        binding.btnRename.setOnClickListener { showRenameDialog() }
        binding.btnUpload.setOnClickListener { startUpload() }
        binding.btnDiscard.setOnClickListener { confirmDiscard() }
        binding.btnCancelUpload.setOnClickListener { cancelUpload() }

        renderDraft()

        if (autoUpload) {
            startUpload()
        }
    }

    override fun onResume() {
        super.onResume()
        // The draft might have been edited from another path while
        // we were paused (rename via web — the local copy doesn't
        // know yet — but the meta change is single-source-of-truth
        // local, so this is mostly a "did the user delete from
        // elsewhere" guard).
        if (draft?.dir?.exists() != true) {
            finish()
            return
        }
        renderDraft()
    }

    override fun onDestroy() {
        // Don't hard-cancel the upload if the activity is killed —
        // the user expects "Upload now" to be durable across screen
        // rotations etc. lifecycleScope already binds the upload
        // to the activity's lifecycle, so the OS-level kill takes
        // care of it; we just don't reach in to abort here.
        super.onDestroy()
    }

    private fun renderDraft() {
        val d = draft ?: return
        val m = d.meta
        binding.draftName.text = m.name ?: getString(R.string.draft_unnamed)
        binding.framesValue.text = getString(R.string.detail_frames_fmt, m.frame_count)
        binding.createdValue.text = m.created_at
        binding.statusValue.text = getString(
            if (m.finalized) R.string.draft_status_ready
            else R.string.draft_status_incomplete
        )
    }

    private fun showRenameDialog() {
        val d = draft ?: return
        val padding = (24 * resources.displayMetrics.density).toInt()
        val input = EditText(this).apply {
            hint = getString(R.string.rename_capture_hint)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_WORDS
            setText(d.meta.name.orEmpty())
            setSelection(text.length)
        }
        val container = android.widget.FrameLayout(this).apply {
            setPadding(padding, padding / 2, padding, 0)
            addView(input)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.rename_capture_title)
            .setMessage(R.string.draft_rename_help)
            .setView(container)
            .setPositiveButton(R.string.action_save) { _, _ ->
                val newName = input.text.toString().trim()
                d.setName(newName.ifEmpty { null })
                renderDraft()
            }
            .setNegativeButton(R.string.action_cancel, null)
            .show()
    }

    private fun confirmDiscard() {
        val d = draft ?: return
        AlertDialog.Builder(this)
            .setTitle(R.string.draft_discard_title)
            .setMessage(R.string.draft_discard_body)
            .setPositiveButton(R.string.finish_action_discard) { _, _ ->
                d.delete()
                finish()
            }
            .setNegativeButton(R.string.action_cancel, null)
            .show()
    }

    private fun startUpload() {
        if (uploadJob?.isActive == true) return
        val d = draft ?: return
        if (baseUrl.isBlank()) {
            Toast.makeText(this, getString(R.string.upload_no_studio), Toast.LENGTH_LONG).show()
            return
        }
        if (d.meta.frame_count == 0) {
            Toast.makeText(this, getString(R.string.upload_no_frames), Toast.LENGTH_SHORT).show()
            return
        }

        // Make sure the draft is finalized — Save-for-later already
        // sets it, but this path also handles a draft the user
        // tapped Upload Now on directly from the home list, where
        // it might be `finalized = false` if the user backed out
        // of the capture activity instead of using Finish.
        if (!d.meta.finalized) d.finalize()

        showUploading(progress = 0, total = d.meta.frame_count)

        val client = StudioClient(baseUrl)
        val uploader = DraftUploader(this, baseUrl, client)
        uploadJob = lifecycleScope.launch {
            val result = uploader.upload(this, d) { sent, total ->
                runOnUiThread { showUploading(sent, total) }
            }
            when (result) {
                is DraftUploader.Result.Ok -> {
                    Toast.makeText(
                        this@DraftDetailActivity,
                        getString(R.string.upload_succeeded),
                        Toast.LENGTH_SHORT,
                    ).show()
                    routeToCaptureDetail(result.captureId)
                }
                is DraftUploader.Result.Failed -> {
                    showActions()
                    AlertDialog.Builder(this@DraftDetailActivity)
                        .setTitle(R.string.upload_failed_title)
                        .setMessage(result.reason)
                        .setPositiveButton(R.string.action_back, null)
                        .show()
                }
            }
        }
    }

    private fun cancelUpload() {
        uploadJob?.cancel()
        uploadJob = null
        showActions()
    }

    private fun showUploading(progress: Int, total: Int) {
        binding.actionRow.visibility = View.GONE
        binding.uploadRow.visibility = View.VISIBLE
        binding.uploadProgress.max = total
        binding.uploadProgress.progress = progress
        binding.uploadLabel.text = getString(
            R.string.upload_progress_fmt,
            progress,
            total,
        )
    }

    private fun showActions() {
        binding.actionRow.visibility = View.VISIBLE
        binding.uploadRow.visibility = View.GONE
    }

    private fun routeToCaptureDetail(captureId: String) {
        val home = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val detail = Intent(this, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, captureId)
            // Server will have filled in a name (auto-generated if
            // the draft's name was null); the detail screen
            // re-fetches via /api/captures/{id} so we just send the
            // local name as a placeholder during the brief
            // "loading…" window.
            putExtra(
                CaptureDetailActivity.EXTRA_CAPTURE_NAME,
                draft?.meta?.name.orEmpty(),
            )
        }
        startActivities(arrayOf(home, detail))
        finish()
    }
}
