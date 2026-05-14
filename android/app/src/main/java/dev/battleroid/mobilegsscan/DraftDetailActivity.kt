package dev.battleroid.mobilegsscan

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
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
    private var serviceCollector: Job? = null

    // Android 13+ runtime permission. The OS shows the system
    // sheet exactly once per uninstall; subsequent calls into
    // launch() short-circuit. If the user denies, the upload
    // still runs but they don't get the progress / completion
    // notifications — we don't block the upload on it.
    private val notifPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { _ -> /* fire-and-forget */ }

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
        } else {
            // Re-attach to a service-side run that may have been
            // started before this activity instance was created
            // (user uploaded, backgrounded, came back). Idempotent
            // when nothing's running — the flow yields null and
            // the UI keeps the idle state.
            bindToUploadServiceState(
                draftId = draftId,
                totalSeed = draft?.meta?.frame_count ?: 0,
            )
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

        // Best-effort permission request. The upload itself goes
        // ahead either way — the user just won't see progress /
        // completion notifications if they decline.
        maybeRequestNotificationPermission()

        state.update {
            it.copy(
                upload = UploadProgress(sent = 0, total = d.meta.frame_count),
                uploadError = null,
            )
        }

        // Hand the upload off to UploadService so it survives
        // backgrounding / lock / dim / sleep. The activity stays
        // bound to the service's state flow for live UI updates
        // while it's in the foreground.
        UploadService.start(this, baseUrl, d.id)
        bindToUploadServiceState(d.id, totalSeed = d.meta.frame_count)
    }

    private fun cancelUpload() {
        UploadService.cancel(this, draft?.id.orEmpty())
        uploadJob?.cancel()
        uploadJob = null
        serviceCollector?.cancel()
        serviceCollector = null
        state.update { it.copy(upload = null) }
    }

    private fun bindToUploadServiceState(draftId: String, totalSeed: Int) {
        // Collect from the service's per-draft state flow only
        // while the activity is at least STARTED. ``lifecycleScope``
        // alone stays active across STOPPED, which would let a
        // backgrounded activity fire ``routeToCaptureDetail`` ->
        // ``startActivities()`` from the background. That's a
        // background-activity-start violation on API 29+ (the
        // intent is dropped silently) and a UX wart on older
        // versions (the task gets yanked to the foreground out
        // from under the user). ``repeatOnLifecycle(STARTED)``
        // pauses the collector on STOPPED and re-runs it on
        // START; since ``stateFlow`` is a StateFlow it replays
        // its latest value on re-subscribe, so a Done that
        // arrived while we were backgrounded still routes
        // correctly when the user returns.
        serviceCollector?.cancel()
        serviceCollector = lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                UploadService.stateFlow(draftId).collect { s ->
                    when (s) {
                        is UploadService.UploadState.Running -> {
                            val total = if (s.total > 0) s.total else totalSeed
                            state.update {
                                it.copy(
                                    upload = UploadProgress(sent = s.sent, total = total),
                                    uploadError = null,
                                )
                            }
                        }
                        is UploadService.UploadState.Done -> {
                            state.update { it.copy(upload = null) }
                            Toast.makeText(
                                this@DraftDetailActivity,
                                getString(R.string.upload_succeeded),
                                Toast.LENGTH_SHORT,
                            ).show()
                            routeToCaptureDetail(s.captureId)
                        }
                        is UploadService.UploadState.Failed -> {
                            state.update {
                                it.copy(upload = null, uploadError = s.reason)
                            }
                        }
                        null -> { /* idle */ }
                    }
                }
            }
        }
    }

    private fun maybeRequestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
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
