package dev.battleroid.mobilegsscan

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.lifecycle.lifecycleScope
import dev.battleroid.mobilegsscan.ui.draft.DraftDetailScreen
import dev.battleroid.mobilegsscan.ui.draft.DraftDetailUiState
import dev.battleroid.mobilegsscan.ui.draft.UploadProgress
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Local-draft detail screen — Compose port of the legacy XML
 * `DraftDetailActivity`.
 *
 * Lists the draft's frame count + on-disk size + created timestamp,
 * lets the user rename it (the local name carries over to the
 * server name on upload), and offers Upload Now / Discard. While
 * an upload is in flight, the action row collapses into a progress
 * bar + cancel button.
 *
 * Auto-upload mode: when launched with EXTRA_AUTO_UPLOAD=true the
 * activity kicks off the upload immediately (CaptureActivity's
 * "Upload now" choice). Without the flag (the home-screen drafts
 * list path), the user has to tap Upload Now explicitly.
 *
 * On successful upload (server acked) the local draft directory
 * is deleted and we route forward to the server-side
 * [CaptureDetailActivity]. On failure the draft stays in place
 * and the error surfaces in a Compose Material3 AlertDialog
 * driven by [DraftDetailUiState.uploadError].
 */
class DraftDetailActivity : ComponentActivity() {
    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_DRAFT_ID = "draft_id"
        const val EXTRA_AUTO_UPLOAD = "auto_upload"
    }

    private val state: MutableStateFlow<DraftDetailUiState> =
        MutableStateFlow(DraftDetailUiState.Initial)
    private val uiState: StateFlow<DraftDetailUiState> = state.asStateFlow()
    private var draft: Draft? = null
    private var baseUrl: String = ""
    private var uploadJob: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

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

        refreshState()

        setContent {
            PebbleTheme {
                val current by uiState.collectAsState()
                DraftDetailScreen(
                    state = current,
                    onBackClick = { finish() },
                    onRenameSubmit = ::onRenameSubmit,
                    onUploadClick = ::startUpload,
                    onCancelUploadClick = ::cancelUpload,
                    onDiscardConfirmed = ::onDiscardConfirmed,
                    onUploadErrorDismiss = {
                        state.update { it.copy(uploadError = null) }
                    },
                )
            }
        }

        if (autoUpload) {
            startUpload()
        }
    }

    override fun onResume() {
        super.onResume()
        // The draft might have been deleted from another path while
        // we were paused (rare but possible if user discarded from
        // a parallel surface). Bail if the dir is gone.
        if (draft?.dir?.exists() != true) {
            finish()
            return
        }
        refreshState()
    }

    private fun refreshState() {
        val d = draft ?: return
        // Total on-disk bytes — sum the JPEG file sizes plus the
        // poses jsonl if it exists. Cheap I/O even for hundreds of
        // frames since File.length() is a stat call, not a read.
        val bytes = d.frameFiles().sumOf { it.length() } +
            (d.posesFileOrNull()?.length() ?: 0L)
        val thumb = d.thumbnailFile()
        state.update {
            it.copy(meta = d.meta, totalBytes = bytes, thumbnailFile = thumb)
        }
    }

    private fun onRenameSubmit(newName: String?) {
        val d = draft ?: return
        d.setName(newName)
        refreshState()
    }

    private fun onDiscardConfirmed() {
        draft?.delete()
        finish()
    }

    private fun startUpload() {
        if (uploadJob?.isActive == true) return
        val d = draft ?: return
        if (baseUrl.isBlank()) {
            Toast.makeText(this, getString(R.string.upload_no_studio), Toast.LENGTH_LONG)
                .show()
            return
        }
        if (d.meta.frame_count == 0) {
            Toast.makeText(this, getString(R.string.upload_no_frames), Toast.LENGTH_SHORT)
                .show()
            return
        }

        // Make sure the draft is finalized — Save-for-later already
        // sets it, but the Upload-Now-from-home-list path may not
        // have if the user backed out of the capture activity
        // instead of using Finish.
        if (!d.meta.finalized) d.finalize()

        state.update {
            it.copy(
                upload = UploadProgress(sent = 0, total = d.meta.frame_count),
                uploadError = null,
            )
        }

        val client = StudioClient(baseUrl)
        val uploader = DraftUploader(this, baseUrl, client)
        uploadJob = lifecycleScope.launch {
            val result = uploader.upload(this, d) { sent, total ->
                state.update {
                    it.copy(upload = UploadProgress(sent = sent, total = total))
                }
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
                    state.update {
                        it.copy(upload = null, uploadError = result.reason)
                    }
                }
            }
        }
    }

    private fun cancelUpload() {
        uploadJob?.cancel()
        uploadJob = null
        state.update { it.copy(upload = null) }
    }

    private fun routeToCaptureDetail(captureId: String) {
        val home = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val detail = Intent(this, CaptureDetailActivity::class.java).apply {
            putExtra(CaptureDetailActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureDetailActivity.EXTRA_CAPTURE_ID, captureId)
            // Server will have filled in a name (auto-generated if
            // the draft's name was null); CaptureDetail re-fetches
            // via /api/captures/{id}, so we just seed with the local
            // name as a placeholder during the brief "loading…"
            // window.
            putExtra(
                CaptureDetailActivity.EXTRA_CAPTURE_NAME,
                draft?.meta?.name.orEmpty(),
            )
        }
        startActivities(arrayOf(home, detail))
        finish()
    }
}
